"""
FC Mobile League Bot — main entry point.
All Telegram command handlers live here.
"""
import asyncio
import html
import io
import json
import logging
import math
import os
import random
import re

from ocr import canonical_nick
import tempfile
import time
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram import LinkPreviewOptions
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

import database as db
from database import (
    init_db,
    upsert_player,
    get_player,
    get_player_by_id,
    get_player_by_telegram_id,
    get_player_by_game_nickname,
    find_players_by_fuzzy_game_nickname,
    get_all_players,
    get_active_tournament,
    get_active_tournaments,
    create_tournament,
    get_tournament,
    update_tournament,
    add_player_to_tournament,
    is_player_in_tournament,
    remove_player_from_tournament,
    get_tournament_players,
    get_pending_match,
    get_match,
    update_match,
    get_tournament_matches,
    get_overdue_matches,
    get_upcoming_deadline_matches,
    get_player_matches,
    set_game_nickname,
    ban_player,
    unban_player,
    is_player_banned,
    adjust_player_elo,
    set_player_elo,
    get_tournament_leaderboard,
    get_tournament_elo,
    set_tournament_chat,
    unset_tournament_chat,
    get_tournament_by_chat,
    find_tournaments_by_name_substring,
    grant_bot_admin,
    revoke_bot_admin,
    is_bot_admin_db,
    list_bot_admins,
    add_tournament_admin,
    remove_tournament_admin,
    is_tournament_admin,
    list_tournament_admins,
    log_tournament_action,
    list_tournament_audit_log,
    get_open_matches_for_player,
    get_h2h_matches,
    get_existing_group_match,
    count_group_matches_for_pair,
)
from telegram.error import TelegramError
from elo import rank_label
from tournament import (
    ALL_PLAYOFF_STAGES,
    PLAYOFF_STAGES,
    draw_groups,
    generate_group_fixtures,
    format_standings_message,
    generate_playoff,
    format_playoff_bracket,
    check_groups_complete,
    compute_playoff_preview,
)
from match_processor import apply_result, apply_walkover
from ocr import (
    parse_match_screenshot,
    detect_tournament_type_from_caption,
    AI_FALLBACK_MODELS,
)
from standings_image import render_standings_png

TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN (or TELEGRAM_BOT_TOKEN) env var is required. "
        "On Railway: project Settings → Variables → add it. "
        "Locally: copy .env.example to .env and fill it in."
    )

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Build / version metadata so ``/version`` can prove which deploy is live.
# We try multiple sources because Railway and other PaaS strip the ``.git``
# directory from runtime images by design. In order of preference:
#
# 1. Env vars set by the platform (Railway: RAILWAY_GIT_COMMIT_SHA, etc.).
# 2. ``git rev-parse HEAD`` if a checkout is available (local/dev).
# 3. A static fallback so the command always returns *something*.
# ─────────────────────────────────────────────────────────────────────────────

BOT_STARTED_AT = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def _resolve_build_info() -> dict:
    info: dict = {
        "commit": "unknown",
        "branch": os.environ.get("RAILWAY_GIT_BRANCH")
        or os.environ.get("GIT_BRANCH")
        or "unknown",
        "message": "",
        "source": "fallback",
    }
    for env_key in (
        "RAILWAY_GIT_COMMIT_SHA",
        "RAILWAY_GIT_COMMIT_HASH",
        "GIT_COMMIT",
        "COMMIT_SHA",
        "VERCEL_GIT_COMMIT_SHA",
    ):
        v = os.environ.get(env_key)
        if v:
            info["commit"] = v[:12]
            info["source"] = f"env:{env_key}"
            msg = (
                os.environ.get("RAILWAY_GIT_COMMIT_MESSAGE")
                or os.environ.get("GIT_COMMIT_MESSAGE")
                or ""
            )
            if msg:
                info["message"] = msg.splitlines()[0][:140]
            return info
    # Local / dev fallback: try git
    try:
        import subprocess
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, timeout=2,
        ).decode().strip()
        if commit:
            info["commit"] = commit[:12]
            info["source"] = "git"
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_dir, timeout=2,
            ).decode().strip()
            if branch and branch != "HEAD":
                info["branch"] = branch
        except Exception:
            pass
        try:
            msg = subprocess.check_output(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=repo_dir, timeout=2,
            ).decode().strip()
            if msg:
                info["message"] = msg[:140]
        except Exception:
            pass
    except Exception:
        pass
    return info


BUILD_INFO = _resolve_build_info()
log.info(
    "Bot build: commit=%s branch=%s source=%s",
    BUILD_INFO["commit"], BUILD_INFO["branch"], BUILD_INFO["source"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
#
# Phase 1 of the bot.py split: small, dependency-light helpers and the
# ``ADMIN_IDS`` list now live in ``handlers.common`` so handler modules
# (added in subsequent phases) can use them without circular imports.
# Re-export them here so existing call-sites in this file (and any
# external imports of e.g. ``from bot import is_admin``) keep working.
# ─────────────────────────────────────────────────────────────────────────────

from handlers.common import (  # noqa: E402  (imports kept after env validation by design)
    ADMIN_IDS,
    _fmt_minute_local,
    _tz_label,
    is_admin,
    is_owner,
    is_root_admin,
    mention,
    arrow,
    t_type_label,
    t_scope_label,
    t_full_label,
    send,
    parse_ban_duration,
    parse_tournament_type_arg,
    _fmt_dt,
    _fmt_date,
    _fmt_minute,
    check_required_channel,
)


def resolve_tournament_for_player(player: dict, tournament_type: str | None = None):
    """
    Pick the most relevant active tournament for a player.

    Preference order:
    1. Active tournament of the requested type (if specified) where the player participates.
    2. Active tournament of the requested type (any participants).
    3. Most recent active tournament where the player participates.
    4. Any most recent active tournament.
    """
    actives = get_active_tournaments()
    if tournament_type:
        candidates = [t for t in actives if t["tournament_type"] == tournament_type]
    else:
        candidates = actives

    pid = player["id"]
    # Prefer one the player joined
    for t in candidates:
        members = get_tournament_players(t["id"])
        if any(m["player_id"] == pid for m in members):
            return t
    # Otherwise return the most recent of the candidate set
    return candidates[0] if candidates else None


# Phase 2 of the bot.py split: cross-cutting tournament helpers now live
# in ``handlers._helpers``. Re-exported below for backward compatibility.
# Phase 3 added a few more leaf helpers (``_resolve_player_arg``,
# ``_format_deadline_countdown``, ``_STAGE_RU``).
from handlers._helpers import (  # noqa: E402
    _STAGE_RU,
    _can_manage_tournament,
    _format_deadline_countdown,
    _player_from_user,
    _resolve_player_arg,
    _resolve_tournament_from_args,
    _tournament_picker_kb,
    _user_active_tournaments,
)

# Phase 2 also moves all admin command handlers (bot-admin + tournament-admin)
# into ``handlers.admin``. Re-exported here so the registration in ``main()``
# below and any external ``from bot import cmd_ban`` keeps working.
from handlers.admin import (  # noqa: E402
    cmd_admin_addgoal,
    cmd_admin_addplayer,
    cmd_admin_addplayer_late,
    cmd_admin_delgoal,
    cmd_admin_matchgoals,
    cmd_admin_setgoalauthor,
    cmd_admin_setgoalname,
    cmd_admin_setnick,
    cmd_admins,
    cmd_add_tadmin,
    cmd_relink_player,
    cmd_cl_spawn_cups,
    cmd_recompute_standings,
    cmd_ban,
    cmd_banned,
    cmd_broadcast,
    cmd_clear_channel,
    cmd_clear_tournament_bg,
    cmd_elo,
    cmd_give_owner,
    cmd_grant_admin,
    cmd_award,
    cmd_revoke_award,
    cmd_awards,
    cmd_owners,
    cmd_remove_tadmin,
    cmd_revoke_admin,
    cmd_revoke_owner,
    cmd_set_channel,
    cmd_set_description,
    cmd_set_owner,
    cmd_set_tournament_bg,
    cmd_setelo,
    cmd_tadmins,
    cmd_unban,
    _resolve_admin_target,
    _resolve_tadmin_target,
    _split_tadmin_args,
    TOURNAMENT_BG_DIR,
    _tournament_bg_path,
)

# Phase 3: read-only query commands (``/h2h``, ``/my_deadlines``, ``/tlog``,
# ``/playoff_preview``) and the dispute flow now live in dedicated modules.
from handlers.queries import (  # noqa: E402
    cmd_h2h,
    cmd_my_deadlines,
    cmd_playoff_preview,
    cmd_tlog,
)
from handlers.profile import (  # noqa: E402
    cmd_admincmd,
    cmd_hide_keyboard,
    cmd_keyboard,
    cmd_matches,
    cmd_myid,
    cmd_profile,
    cmd_register,
    cmd_setnick,
    cmd_show_keyboard,
)
from handlers.leaderboard import (  # noqa: E402
    _build_official_local_view,
    _resolve_leaderboard_tournament,
    _send_feedback_to_admins,
    _send_bug_to_admins,
    _send_top_by_field,
    cmd_cancel,
    cmd_feedback,
    cmd_bug,
    cmd_leaderboard,
    cmd_table_bomb,
    cmd_top,
    cmd_top_ri,
    cmd_top_scorers,
    cmd_top_vsa,
)
from handlers.tournament import (  # noqa: E402
    _bool_arg,
    _can_advance_now,
    _can_bind_tournament,
    _do_finish_tournament,
    _do_simulate_tournament,
    _handle_tournament_settings_cb,
    _parse_add_player_usernames,
    _poisson,
    _recent_finished_tournaments,
    _render_playoff_for,
    _render_table_for,
    _send_tournament_picker,
    _simulated_score,
    _ts_format_panel_text,
    _ts_show_panel,
    cb_advance_now,
    cb_finish_tournament,
    cb_playoff_pick,
    cb_simulate,
    cb_table_pick,
    cb_table_view,
    cmd_add_player,
    cmd_advance_playoff,
    cmd_bind_tournament,
    cmd_clear_groups,
    cmd_create_tournament,
    cmd_finish_tournament,
    cmd_list_players,
    cmd_playoff,
    cmd_playoff_text,
    cmd_prune_phantoms,
    cmd_fill_missing_matches,
    cmd_redraw_groups,
    cmd_replace_player,
    cmd_set_auto_confirm,
    cmd_set_third_place,
    cmd_skip_third_place,
    cmd_set_team,
    cmd_myteam,
    cb_team_buttons,
    handle_pending_team_tag_text,
    cmd_set_penalties,
    cmd_set_group,
    cmd_set_matches_per_pair,
    cmd_set_overlay,
    cmd_set_playoff_slots,
    cmd_set_groupname,
    cmd_clear_groupname,
    cmd_set_reminders,
    cmd_set_signup_reminder,
    cmd_set_signup_link,
    cmd_clear_signup_link,
    cmd_set_signup_deadline,
    cmd_clear_signup_deadline,
    cmd_set_row_alpha,
    cmd_set_series_length,
    cmd_simulate,
    cmd_close_groups,
    cmd_start_playoff,
    cmd_redraw_playoff,
    cmd_start_tournament,
    cmd_table,
    cmd_table_text,
    cmd_tournaments,
    cmd_past_tournaments,
    cmd_tournament_summary,
    cmd_compare_tournaments,
    cmd_tours,
    cmd_tourstext,
    cmd_next_tour,
    cmd_regen_tours,
    cmd_drop_ghost_matches,
    cmd_repair_tour_numbers,
    cmd_post_tours,
    cmd_edit_announce,
    cmd_tour_diag,
    cmd_export_db,
    cmd_import_db,
    cmd_export_bot,
    cb_finished_tournaments,
    cb_tournament_summary_button,
    cb_compare_tournaments,
    cb_reroll_facts,
    cb_db_buttons,
    on_db_import_document,
    cmd_unbind_tournament,
    cmd_set_footer,
    cmd_clear_footer,
)
from handlers.templates import (  # noqa: E402
    handle_pending_tpl_create_text,
)
from handlers.match import (  # noqa: E402
    SCORE_RE,
    _after_opponent_confirm,
    _announce_stage_advance,
    _approver_telegram_ids,
    _current_playoff_stage,
    _do_walkover,
    _finalize_match_after_admin,
    _format_series_line,
    _list_pending_matches_for,
    _maybe_auto_advance,
    _send_match_to_admins,
    _send_failed_screenshot_to_admins,
    cmd_admin_report,
    cmd_admin_photo,
    cmd_reocr,
    cmd_award_points,
    cmd_audit,
    cmd_delete_match,
    cmd_dispute,
    cmd_edit_goals,
    cmd_edit_match,
    cmd_force_confirm,
    cmd_pending,
    cmd_set_match_deadline,
    cmd_tech_draw,
    cmd_tmatches,
    cmd_promote,
    cmd_po_stage_config,
    cmd_walkover,
    cmd_walkover_all,
    cmd_walkover_match,
    cmd_tech_nil_all,
    cmd_withdraw,
)


# ─────────────────────────────────────────────────────────────────────────────
# /start  /help
# ─────────────────────────────────────────────────────────────────────────────

PLAYER_HELP_TEXT = """
⚽ <b>FC Mobile League Bot</b>

<b>Старт / меню / профиль</b>
/start, /elodrak — открыть меню бота
/help — помощь для игроков
/admincmd — помощь для админов
  Алиасы: /admin_cmd, /adminhelp, /admin_help
/myid — показать user_id и chat_id
  Алиасы: /id, /whoami
/keyboard — переключить нижнюю панель
  Алиасы: /kb, /menu_toggle, /toggle_menu
/hide_keyboard — скрыть нижнюю панель
  Алиасы: /hidekeyboard, /hide_menu, /hidemenu
/show_keyboard — показать нижнюю панель
  Алиасы: /showkeyboard, /show_menu, /showmenu
/register — зарегистрироваться в лиге
/setnick <i>ник</i> — указать игровой ник
  Алиас: /set_nick
/profile [@username] — профиль игрока
/matches — твои последние матчи
/my_deadlines — текущие дедлайны твоих матчей
  Алиасы: /mydeadlines, /deadlines
/h2h @user — история личных встреч
  Алиас: /vs
/feedback &lt;текст&gt; — отправить отзыв/баг админам
/cancel — отменить активный визард

<b>Зал славы</b>
/champions — чемпионы прошлых турниров (Гвардиолыч / Фэнтези / VSA) с инлайн-меню
  Алиасы: /champs, /hall_of_fame, /halloffame, /zalslavy
/champion @user — все чемпионства/финалы конкретного игрока
  Алиас: /champ

<b>Турниры (просмотр)</b>
/tournaments — список активных турниров
/list_players [ID] — состав турнира
  Алиас: /listplayers
/table [ID|вса|ри] [all|split|text|&lt;группа&gt;] — турнирная таблица
  Алиас: /standings
/table_text — таблица текстом
  Алиас: /standings_text
/playoff [ID|вса|ри] [text] — сетка плей-офф
  Алиас: /bracket
/playoff_text — текстовая сетка
  Алиас: /bracket_text
/playoff_preview [ID] — предпросмотр сетки плей-офф
  Алиасы: /playoffpreview, /preview_playoff
/leaderboard [ID|вса|ри] — лидерборд турнира
/top — общий ELO-рейтинг
/top_vsa — топ ELO по ВСА
/top_ri — топ ELO по РИ
/top_scorers [all|&lt;tid&gt;] — бомбардиры
  Алиасы: /topscorers, /scorers, /bombardiry
/tablebomb [ID] — бомбардиры турнира (по сторонам)
  Алиасы: /table_bomb, /bomb, /bombardiry_t, /bombs

<b>Результаты матчей</b>
📸 <i>Фото скрина</i> — бот распознает счёт автоматически (OCR)
/report 3:2 @opponent [вса|ри] — результат вручную
/confirm — подтвердить результат
/dispute — оспорить результат
"""

ADMIN_ONLY_HELP_TEXT = """
👮 <b>Админская часть</b>

<b>Управление турниром (создатель/админ)</b>
/create_tournament — визард создания турнира
/add_player @u1[, @u2, ...] — добавить игроков до старта
/replace_player @old @new [ID] — заменить игрока
  Алиас: /replaceplayer
/start_tournament — жеребьёвка и старт
/redraw_groups &lt;ID&gt; — перетряхнуть группы
  Алиас: /redrawgroups
/set_group &lt;ID&gt; @user &lt;группа&gt; — назначить игрока в группу
  Алиас: /setgroup
/clear_groups &lt;ID&gt; — очистить распределение по группам
  Алиас: /cleargroups
/start_playoff [ID|вса|ри] — запустить плей-офф
/redraw_playoff [ID] — пересеять сетку плей-офф «крест по группам»
  Алиас: /redrawplayoff
/advance [ID] — вручную продвинуть плей-офф
  Алиас: /advance_playoff
/finish_tournament [ID] — завершить турнир
  Алиасы: /finishtournament, /end_tournament, /close_tournament
/tournament_summary [ID] [ai] [telegraph] — сводка турнира файлом (.txt) с тем, кто на какой стадии вылетел; флаги: <code>ai</code> — добавить анализ от ИИ, <code>telegraph</code> — опубликовать пост на telegra.ph
  Алиасы: /summary, /svodka, /tournament_report
/past_tournaments — итоги завершённых турниров (с кнопками «📄 Сводка»)
  Алиасы: /finished_tournaments, /itogi, /results
/compare_tournaments — сравнение всех завершённых турниров (топы, рекорды, общая статистика)
  Алиасы: /compare, /sravnenie, /all_tournaments
/simulate [ID] — авто-симуляция оставшихся матчей
  Алиас: /autosim
/bind_tournament &lt;ID&gt; — привязать чат к турниру
/unbind_tournament — отвязать чат
/set_description &lt;текст&gt; — описание турнира
/set_channel @channel — обязательная подписка на канал
/clear_channel — снять условие подписки
/close_groups [ID|вса|ри] — закрыть групповой этап

<b>Настройки турнира</b>
/set_playoff_slots &lt;ID&gt; &lt;N&gt; — сколько выходят из группы
  Алиасы: /setplayoffslots, /playoff_slots
/set_series_length &lt;ID&gt; &lt;N&gt; — длина серии (бо-N)
  Алиасы: /setserieslength, /series_length
/set_auto_confirm &lt;ID&gt; on|off — автоподтверждение
  Алиасы: /setautoconfirm, /auto_confirm
/set_third_place &lt;ID&gt; on|off — матч за 3-е место
  Алиасы: /setthirdplace, /third_place
/set_penalties &lt;ID&gt; on|off — учитывать пенальти при ничье в плей-офф
  Алиасы: /setpenalties, /penalties
/set_matches_per_pair &lt;ID&gt; group|playoff &lt;N&gt; — матчей в паре
  Алиасы: /setmatchesperpair, /matches_per_pair
/set_reminders &lt;ID&gt; ... — настройки напоминаний
  Алиасы: /setreminders, /reminders

<b>Кастомизация</b>
/set_tournament_bg [ID] — задать кастомный фон для таблиц/сеток
  Алиасы: /settournamentbg, /set_bg, /tournament_bg
/clear_tournament_bg [ID] — удалить кастомный фон
  Алиасы: /cleartournamentbg, /clear_bg
/set_overlay &lt;ID&gt; &lt;0-100&gt; — прозрачность затемнения фона
  Алиасы: /setoverlay, /overlay, /set_transparency, /settransparency
/set_row_alpha &lt;ID&gt; &lt;0-100&gt; — прозрачность строк/карточек таблицы и сетки
  Алиасы: /setrowalpha, /row_alpha, /rowalpha

<b>Участники (по ходу турнира)</b>
/admin_addplayer_late &lt;tid&gt; @user [группа] — подсадить игрока
  Алиасы: /addplayer_late, /joinlate, /add_participant, /add_player
/withdraw &lt;tid&gt; @user [причина] — снять игрока
  Алиасы: /kick_player, /remove_participant, /remove_player
/replace_player @old @new [ID] — заменить игрока

<b>Результаты (админ)</b>
/pending [tid] [@user] — список pending-матчей
  Алиасы: /pending_matches, /pendingmatches
/audit [tid] [@admin] [page] — аудит-лог турнира с кнопками отмены
  Алиасы: /tlog, /auditlog, /audit_log
  Фильтр: <code>/audit 6 auto</code> — только авто-ТП
  Фильтр: <code>/audit 6 @username</code> — действия конкретного админа
/walkover @loser [@winner] [ID] — техническое поражение
  Алиас: /tp
/walkover #&lt;match_id&gt; [@loser] — ТП по ID матча
/walkover_match &lt;match_id&gt; [@loser] — алиас
  Алиас: /walkovermatch
/walkover_all @loser [tid] — ТП всем матчам игрока
  Алиасы: /walkoverall, /tp_all, /tpall
/tech_nil_all [tid] — технический ноль (0:0) всем оставшимся матчам турнира
  Алиасы: /tn_all, /tnall, /technilall
/promote @player [tid] — принудительно продвинуть игрока
  Алиасы: /force_advance, /advance_player
/po_stage_config &lt;stage&gt; &lt;bo3|bo5|bo7&gt; [wins|goals] [tid] — формат стадии плей-офф
  Алиасы: /po_stage, /po_format
/tech_draw @p1 @p2 [X:X] [tid] — техническая ничья
  Также: <code>/tech_draw #&lt;match_id&gt; [X:X]</code>
  Алиасы: /techdraw, /draw, /td
/set_deadline #&lt;match_id&gt; +&lt;часы&gt; — продлить дедлайн
  Также: <code>/set_deadline #&lt;match_id&gt; YYYY-MM-DD HH:MM</code>
  По паре: <code>/set_deadline @p1 @p2 +24 [tid]</code>
  Массово: <code>/set_deadline group|playoff &lt;tid&gt; +N</code>
  По стадии: <code>/set_deadline r16|qf|sf|final &lt;tid&gt; +N</code>
  Алиасы: /setdeadline, /set_dd, /setdd, /dd, /change_deadline, /changedeadline
/admin_report @u1 @u2 3:2 [ID] — внести результат за игроков
  Алиасы: /adminreport, /force_report
/admin_photo @u1 @u2 [ID] — ответом на фото: OCR счёт + записать матч
  Поддержка альбомов: все фото из альбома обрабатываются
  Алиасы: /adminphoto, /photo_report, /photoreport
/reocr @u1 @u2 [ID] — ответом на фото: пересчитать через Tesseract (без AI)
  Алиасы: /re_ocr, /tessocr, /tess_ocr
/award_points @user N [ID] [причина] — выдать/снять очки
  Алиасы: /awardpoints, /give_points, /givepoints
/edit_goals #&lt;match_id&gt; @sc1 @sc2 ... — переписать бомбардиров
  Алиасы: /editgoals, /set_goals, /setgoals
/admin_matchgoals &lt;match_id&gt; — список голов матча
  Алиас: /adminmatchgoals, /matchgoals
/admin_addgoal &lt;match_id&gt; [@user|home|away] [home|away] [мин] [name:&lt;имя&gt;] — добавить гол
  Без @user (только сторона + name:) — для матчей, занесённых счётом.
  Алиасы: /adminaddgoal, /addgoal
/admin_delgoal &lt;goal_id&gt; — удалить гол
  Алиасы: /admindelgoal, /delgoal
/admin_setgoalauthor &lt;goal_id&gt; @user — переназначить автора гола
  Алиасы: /adminsetgoalauthor, /setgoalauthor, /admin_reassign_goal, /reassign_goal
/admin_setgoalname &lt;goal_id&gt; &lt;имя&gt; — изменить имя футболиста у гола
  Алиасы: /setgoalname, /renamegoal, /admin_renamegoal
/prune_phantoms [tid|all] — удалить фантомные матчи
  Алиасы: /prunephantoms, /clean_phantoms
/ocr_compare — сравнить OCR-модели (ответом на скрин)
  Алиас: /ocrcompare
/test_ocr — прогнать только tesseract по скрину (без AI)
  Алиас: /testocr
/edit_match #&lt;match_id&gt; &lt;X:Y&gt; — переписать счёт уже сыгранного матча
  Алиас: /editmatch
/delete_match #&lt;match_id&gt; — удалить матч полностью
  Алиас: /deletematch
/force_confirm #&lt;match_id&gt; — принудительно зачесть зависший матч
  Алиас: /forceconfirm
/tmatches [tid] [@user] [группа] — список матчей турнира по игрокам
  Алиасы: /t_matches, /tournament_matches
/fill_missing_matches [tid] — добить пропущенные pending-матчи группы
  Алиасы: /fillmissing, /fill_matches
/recompute_standings [tid] — пересчитать таблицу из матчей (чинит лишние игры)
  Алиасы: /recalc_standings, /recompute_table

<b>ELO / баны / пользователи</b>
/elo @user +50 [причина] — изменить ELO на дельту
/setelo @user 200 [причина] — задать абсолютный ELO
/ban @user [длительность] [причина] — забанить
/unban @user — разбанить
/banned — список забаненных
/admin_setnick @user &lt;ник&gt; — задать ник игроку
  Алиасы: /adminsetnick, /setnick_for, /setnickfor
/relink_player @oldhandle &lt;telegram_id&gt; — слить две записи
  Алиасы: /relinkplayer, /relink, /merge_player, /mergeplayer
/cl_spawn_cups &lt;league_id&gt; [main_size] [cons_size] — после ЛЧ-лиги: создать основной кубок + Лигу Конфети
  Алиасы: /clspawncups, /spawn_cups
/grant_admin @user [коммент] — выдать админку
  Алиас: /grantadmin
/revoke_admin @user — снять админку
  Алиас: /revokeadmin
/admins — список админов
/add_tadmin &lt;ID&gt; @user — добавить админа турнира
  Алиас: /addtadmin
/remove_tadmin &lt;ID&gt; @user — убрать админа турнира
  Алиас: /removetadmin
/tadmins [ID] — список админов турнира
/tlog [ID] — аудит-лог турнира
  Алиасы: /tournament_log, /tournamentlog
/broadcast &lt;текст&gt; — рассылка участникам
  Алиас: /announce
/give_owner [ID] @user — передать владение турниром
  Алиасы: /giveowner, /transfer_owner, /transferowner
/owner @user — назначить владельца бота (суперадмин)
  Алиасы: /setowner, /set_owner
  Только для текущих владельцев и root-админов (ADMIN_IDS).
/revoke_owner @user — снять владельца бота
  Алиас: /revokeowner
  Только для текущих владельцев и root-админов (ADMIN_IDS).
/owners — список владельцев бота

<b>Зал славы (импорт из канала)</b>
/alias add "Имя" @user — добавить алиас (free-form имя → игрок)
/alias list [@user] — список алиасов (всех или одного игрока)
/alias remove "Имя" — удалить алиас
  Алиас команды: /aliases
/import_champions [path] — массовый импорт чемпионов из JSON
  По умолчанию читает <code>data/champions_parsed.json</code>;
  попутно автоприменяет <code>data/aliases_to_review.json</code>,
  если в нём заполнены <code>suggested_username</code>.
  Идемпотентно — повторный запуск обновляет существующие записи.
  Алиас: /importchampions
/rename_champion &lt;игрок&gt; &lt;Новый ник&gt; — переименовать игрока в зале славы
  Меняет game_nickname; видно в /champions сразу после смены.
  Игрок: @user, Telegram ID, текущий ник или <code>id=&lt;players.id&gt;</code>.
  Алиасы: /renamechampion, /champ_setnick, /champion_setnick
/add_trophy &lt;игрок&gt; [main|fantasy|vsa|supercup] [YYYY-MM-DD|today] [#N] [X:Y] [заметки]
  Добавить трофей вручную. По умолчанию <b>main</b> и сегодняшняя дата.
  Аргументы [type]/[date]/[#N]/[X:Y] можно в любом порядке.
  Алиасы: /addtrophy, /trophy_add
/list_trophies &lt;игрок&gt; — все трофеи игрока с их внутренними ID
  Нужно перед /remove_trophy, чтобы знать какой ID удалять.
  Алиасы: /listtrophies, /trophies
/remove_trophy &lt;id&gt; — удалить запись о трофее по ID
  ID берётся из /list_trophies или из ответа /add_trophy.
  Алиасы: /removetrophy, /del_trophy, /trophy_remove

<b>Авто-шутки (LLM)</b>
/jokes — открыть меню настроек авто-шуток (всё через кнопки)
  Алиас: /jokes_menu. Включение, интервал, режим, контекст, порог,
  модель (root) — всё внутри панели.
/joke — выдать шутку сейчас по последним сообщениям этого чата
  Cooldown 60 сек на чат. Сначала включи в /jokes.
/jokes_history [N] — последние N шуток (доступно всем)
"""
# ADMIN_HELP_TEXT). Kept as the concatenation, BUT note that we never send
# this as a single Telegram message — Telegram's hard limit is 4096 chars
# and the combined text is well over that. See `cmd_help` which sends in
# two parts.
ADMIN_HELP_TEXT = PLAYER_HELP_TEXT.rstrip() + "\n" + ADMIN_ONLY_HELP_TEXT
HELP_TEXT = ADMIN_HELP_TEXT


# Telegram hard limit on a single text message.
TG_MAX_MESSAGE_CHARS = 4096


def help_text_for(user_id: int) -> str:
    """
    Help text for ``/help``. Always returns just the *player-facing* commands
    — admins see admin-only commands via ``/admincmd`` (or its alias
    ``/adminhelp``). Keeping these split avoids overwhelming non-admin
    users and stays well under Telegram's 4096-char limit even for the
    player section by itself.
    """
    if is_admin(user_id):
        return PLAYER_HELP_TEXT.rstrip() + (
            "\n\nℹ️ Ты админ — список админ-команд: /admincmd"
        )
    return PLAYER_HELP_TEXT


def admin_help_text() -> str:
    """Admin-only command reference (shown by ``/admincmd``)."""
    return ADMIN_ONLY_HELP_TEXT


def _split_for_telegram(text: str, limit: int = TG_MAX_MESSAGE_CHARS) -> list[str]:
    """
    Split ``text`` into chunks that fit Telegram's per-message limit, trying
    hard to break on blank-line boundaries so HTML tags aren't sliced.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Prefer a blank-line boundary; fall back to newline; last resort hard-cut.
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


# ─────────────────────────────────────────────────────────────────────────────
# Menu system: main reply keyboard + inline submenus + wizard state machine
# ─────────────────────────────────────────────────────────────────────────────

# Main menu button labels
M_TOURNAMENTS = "🏆 Турниры"
M_PROFILE     = "👤 Профиль"
M_REPORT      = "📨 Репорт"
M_TOP         = "📊 Топ"
M_SETTINGS    = "🔧 Настройки"
M_QUOTES      = "💬 Цитаты"
M_FEEDBACK    = "🐞 Связь"
M_ADMIN       = "👮 Админ"
M_HELP        = "ℹ️ Помощь"

MENU_LABELS = {
    M_TOURNAMENTS, M_PROFILE, M_REPORT, M_TOP,
    M_SETTINGS, M_QUOTES, M_FEEDBACK, M_ADMIN, M_HELP,
}


def main_menu_kb(user_id: int | None = None) -> ReplyKeyboardMarkup:
    """Bottom reply keyboard, persistent across messages."""
    rows = [
        [KeyboardButton(M_TOURNAMENTS), KeyboardButton(M_PROFILE)],
        [KeyboardButton(M_REPORT),      KeyboardButton(M_TOP)],
        [KeyboardButton(M_SETTINGS),    KeyboardButton(M_QUOTES)],
        [KeyboardButton(M_FEEDBACK)],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([KeyboardButton(M_ADMIN), KeyboardButton(M_HELP)])
    else:
        rows.append([KeyboardButton(M_HELP)])
    # In groups Telegram requires `selective=True` to bind the keyboard to a
    # specific user (so each member can navigate independently). We always
    # set selective; in DMs it's a no-op.
    return ReplyKeyboardMarkup(
        rows, resize_keyboard=True, is_persistent=True, selective=True
    )


def _menu_kb_for(update: Update | None, user_id: int | None = None):
    """
    Return the bottom reply keyboard for the current chat, or None.

    Rules:
      * Group / supergroup / channel chats — never show the bottom keyboard
        (only slash commands work there).
      * If the user toggled it off via /hide_keyboard — also no keyboard.
      * Otherwise — show the standard menu.
    """
    chat = update.effective_chat if update else None
    if chat and chat.type in ("group", "supergroup", "channel"):
        return None
    # DM user opt-out
    if user_id is not None:
        try:
            p = get_player_by_telegram_id(user_id)
            if p and bool(p.get("no_keyboard")):
                return None
        except Exception:
            pass
    return main_menu_kb(user_id)


def _is_group_chat(update: Update | None) -> bool:
    """True if the update's chat is a group/supergroup/channel."""
    chat = update.effective_chat if update else None
    return bool(chat and chat.type in ("group", "supergroup", "channel"))


def main_menu_inline_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Inline-keyboard version of the main menu — used in group chats where
    Telegram's bottom ReplyKeyboard isn't suitable (it'd attach to every
    message and be visible to everyone). Buttons sit in the bot's own
    message and any user can tap them."""
    rows = [
        [
            InlineKeyboardButton(M_TOURNAMENTS, callback_data="gmenu:tournaments"),
            InlineKeyboardButton(M_PROFILE,     callback_data="gmenu:profile"),
        ],
        [
            InlineKeyboardButton(M_REPORT,      callback_data="gmenu:report"),
            InlineKeyboardButton(M_TOP,         callback_data="gmenu:top"),
        ],
        [
            InlineKeyboardButton(M_SETTINGS,    callback_data="gmenu:settings"),
            InlineKeyboardButton(M_QUOTES,      callback_data="gmenu:quotes"),
        ],
        [
            InlineKeyboardButton(M_FEEDBACK,    callback_data="gmenu:feedback"),
        ],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([
            InlineKeyboardButton(M_ADMIN, callback_data="gmenu:admin"),
            InlineKeyboardButton(M_HELP,  callback_data="gmenu:help"),
        ])
    else:
        rows.append([InlineKeyboardButton(M_HELP, callback_data="gmenu:help")])
    return InlineKeyboardMarkup(rows)


def _menu_markup_for(update: Update | None, user_id: int | None = None):
    """Return the appropriate main-menu markup for the current chat:
    inline keyboard in groups, bottom reply keyboard in DMs (or None
    if the DM user opted out)."""
    if _is_group_chat(update):
        return main_menu_inline_kb(user_id)
    return _menu_kb_for(update, user_id)


def _back_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")


def submenu_tournaments(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📋 Список активных", callback_data="t:list")],
        [InlineKeyboardButton("🏁 Итоги турниров",  callback_data="t:finished:0")],
        [InlineKeyboardButton("📊 Сравнить турниры", callback_data="t:compare")],
        [InlineKeyboardButton("📊 Таблица",         callback_data="t:table"),
         InlineKeyboardButton("🏆 Сетка PO",        callback_data="t:bracket")],
        [InlineKeyboardButton("⚽ Бомбардиры",      callback_data="t:bomb")],
        [InlineKeyboardButton("📝 Таблица текстом", callback_data="t:tabletxt"),
         InlineKeyboardButton("📝 Сетка текстом",  callback_data="t:brackettxt")],
    ]
    # "Создать турнир" — только для админов: обычные юзеры не создают
    # кастомные турниры, организацией занимаются только админы.
    if user_id is not None and is_admin(user_id):
        rows.append(
            [InlineKeyboardButton("➕ Создать турнир", callback_data="t:create")]
        )
    return InlineKeyboardMarkup(rows)


def submenu_profile() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Поменять игровой ник", callback_data="p:setnick")],
        [InlineKeyboardButton("📜 Мои матчи",            callback_data="p:matches")],
    ])


def submenu_report() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Через фото скрина",    callback_data="r:photo")],
        [InlineKeyboardButton("⌨️ Ввести вручную",        callback_data="r:manual")],
    ])


def submenu_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Игровой ник",          callback_data="p:setnick")],
        [InlineKeyboardButton("📝 Описание турнира",     callback_data="s:desc")],
        [InlineKeyboardButton("🔗 Канал-условие",        callback_data="s:channel")],
        [InlineKeyboardButton("🧹 Снять канал-условие",  callback_data="s:clear_channel")],
        [InlineKeyboardButton("🏆 Настройки турнира",   callback_data="ts:pick")],
    ])


def submenu_tournament_settings(t: dict) -> InlineKeyboardMarkup:
    """Top-level settings menu — organized into category sub-menus."""
    tid = t["id"]
    import json as _json
    raw_footer = (t.get("footer_text") or "").strip()
    if raw_footer:
        variants = []
        if raw_footer.startswith("["):
            try:
                parsed = _json.loads(raw_footer)
                if isinstance(parsed, list):
                    variants = [v for v in parsed if str(v).strip()]
            except (ValueError, _json.JSONDecodeError):
                pass
        if not variants:
            variants = [raw_footer]
        footer_lbl = f"{len(variants)} вар." if len(variants) > 1 else "задан"
    else:
        footer_lbl = "нет"
    rows = []

    # Surface the follow-up-cups button at the very top when the
    # tournament was created from the Champions League (32) template
    # AND the league has finished (stage='groups_done' or 'finished').
    # When cups are already spawned we show their IDs instead so admins
    # can jump to the bracket without remembering which one is which.
    from tournament import parse_followup_cups_config, parse_followup_cups_tids
    cups_cfg = parse_followup_cups_config(t.get("followup_cups_config"))
    cups_tids = parse_followup_cups_tids(t.get("followup_cups_tids"))
    if cups_cfg:
        stage = (t.get("stage") or "").lower()
        if cups_tids:
            main_tid, cons_tid = cups_tids
            rows.append([InlineKeyboardButton(
                f"🏆 Основной кубок: id {main_tid}",
                callback_data=f"ts:open:{main_tid}",
            )])
            if cons_tid:
                rows.append([InlineKeyboardButton(
                    f"🥉 Лига Конфети: id {cons_tid}",
                    callback_data=f"ts:open:{cons_tid}",
                )])
        elif stage in ("groups_done", "finished"):
            ms = int(cups_cfg.get("main_size", 24))
            cs_raw = cups_cfg.get("consolation_size")
            # Consolation defaults to "all remaining past main_size"
            # so the same template handles 32 / 34 / 36 … rosters.
            from database import get_tournament_players
            try:
                roster = len(get_tournament_players(tid))
            except Exception:
                roster = 0
            if cs_raw is not None:
                cs = int(cs_raw)
            else:
                cs = max(0, roster - ms) if roster else 0
            label = (
                f"🏆 Создать кубки: топ-{ms} + утешение {cs}"
                if cs >= 2 else
                f"🏆 Создать основной кубок (топ-{ms})"
            )
            rows.append([InlineKeyboardButton(
                label,
                callback_data=f"ts:cl_spawn:{tid}",
            )])
        else:
            rows.append([InlineKeyboardButton(
                "🏆 Кубки появятся здесь после окончания лиги",
                callback_data=f"ts:cl_spawn_info:{tid}",
            )])

    rows.extend([
        [InlineKeyboardButton(
            "⚽ Матчи и OCR",
            callback_data=f"ts:cat_match:{tid}",
        )],
        [InlineKeyboardButton(
            "🏆 Плей-офф и формат",
            callback_data=f"ts:cat_playoff:{tid}",
        )],
        [InlineKeyboardButton(
            "🎨 Оформление",
            callback_data=f"ts:cat_style:{tid}",
        )],
        [InlineKeyboardButton(
            "🔔 Напоминания",
            callback_data=f"ts:cat_remind:{tid}",
        )],
        [InlineKeyboardButton(
            "📅 Туры",
            callback_data=f"ts:cat_tours:{tid}",
        )],
        [InlineKeyboardButton(
            f"📝 Подпись к сообщениям: {footer_lbl}",
            callback_data=f"ts:footer:{tid}",
        )],
        [InlineKeyboardButton(
            "📋 Команды турнира",
            callback_data=f"ts:commands:{tid}",
        )],
        [InlineKeyboardButton(
            "🛂 Проверить матчи",
            callback_data=f"ts:review:{tid}",
        )],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ])
    return InlineKeyboardMarkup(rows)


def _submenu_ts_match(t: dict) -> InlineKeyboardMarkup:
    """Sub-menu: match & OCR settings."""
    tid = t["id"]
    auto = "вкл" if int(t.get("auto_confirm") or 0) else "выкл"
    _ocr_mode_label = {
        "ai": "ИИ + тесеракт",
        "ai_no_tess": "только ИИ",
        "score_only": "только счёт",
    }.get(t.get("ocr_mode") or "ai", "ИИ + тесеракт")
    mpg = int(t.get("group_matches_per_pair") or 1)
    mpp = int(t.get("playoff_matches_per_pair") or 1)
    atl_enabled = int(t.get("auto_tech_loss_enabled") or 0)
    atl_lbl = "вкл" if atl_enabled else "выкл"
    rows = [
        [InlineKeyboardButton(
            f"🤖 OCR: {_ocr_mode_label}",
            callback_data=f"ts:ocr:{tid}",
        )],
        [InlineKeyboardButton(
            f"⚡ Автозачёт скринов: {auto}",
            callback_data=f"ts:auto:{tid}",
        )],
        [InlineKeyboardButton(
            f"⚽ Игр в группе (на пару): {mpg}",
            callback_data=f"ts:mpg:{tid}",
        )],
        [InlineKeyboardButton(
            f"🏆 Игр в паре PO: {mpp}",
            callback_data=f"ts:mpp:{tid}",
        )],
        [InlineKeyboardButton(
            f"⏰ Авто-ТП при просрочке: {atl_lbl}",
            callback_data=f"ts:atl:{tid}",
        )],
        [InlineKeyboardButton("⬅️ Назад к настройкам", callback_data=f"ts:open:{tid}")],
    ]
    return InlineKeyboardMarkup(rows)


def _submenu_ts_playoff(t: dict) -> InlineKeyboardMarkup:
    """Sub-menu: playoff, format, structure settings."""
    tid = t["id"]
    slots = int(t.get("playoff_slots") or 0)
    series = int(t.get("series_length") or 1) or 1
    adv_mode = (t.get("playoff_advance_mode") or "wins").lower()
    adv_lbl = "по победам" if adv_mode == "wins" else "по голам"
    signup = int(t.get("open_signup") or 0)
    signup_lbl = "открыта" if signup else "закрыта"
    groups_only = int(t.get("groups_only") or 0)
    bracket_only = int(t.get("bracket_only") or 0)
    fmt_lbl = (
        "только группы" if groups_only
        else "только плей-офф" if bracket_only
        else "группы → плей-офф"
    )
    if groups_only and int(t.get("groups_count") or 2) == 1:
        fmt_lbl = "лига (чемпионат)"
    third = "вкл" if int(t.get("playoff_third_place") or 0) else "выкл"
    pens = "вкл" if int(t.get("playoff_penalties") or 0) else "выкл"

    # If a bronze fixture has been spawned but isn't fully played yet,
    # surface a one-tap cancel button so the admin can unstick the
    # tournament without remembering /skip_third_place.
    bronze_pending = False
    try:
        from tournament import _third_place_complete  # local import: avoid cycle at module load
        bronze_pending = _third_place_complete(int(tid), t) is False
    except Exception:
        bronze_pending = False
    rows = [
        [InlineKeyboardButton(
            f"📅 Формат: {fmt_lbl}",
            callback_data=f"ts:format:{tid}",
        )],
        [InlineKeyboardButton(
            f"🏁 Из группы в плей-офф: топ-{slots or '?'}",
            callback_data=f"ts:slots:{tid}",
        )],
        [InlineKeyboardButton(
            f"🥊 Серия плей-офф: бо-{series}",
            callback_data=f"ts:series:{tid}",
        )],
        [InlineKeyboardButton(
            f"🎯 Проход по: {adv_lbl}",
            callback_data=f"ts:advmode:{tid}",
        )],
        [InlineKeyboardButton(
            f"🥉 Матч за 3-е место: {third}",
            callback_data=f"ts:third:{tid}",
        )],
    ]
    if bronze_pending:
        rows.append([InlineKeyboardButton(
            "❌ Отменить незавершённый матч за 3-е место",
            callback_data=f"ts:third_skip:{tid}",
        )])
    rows.extend([
        [InlineKeyboardButton(
            f"⚽ Пенальти при ничье: {pens}",
            callback_data=f"ts:pen:{tid}",
        )],
        [InlineKeyboardButton(
            f"🙋 Запись: {signup_lbl}",
            callback_data=f"ts:signup:{tid}",
        )],
        [InlineKeyboardButton("⬅️ Назад к настройкам", callback_data=f"ts:open:{tid}")],
    ])
    return InlineKeyboardMarkup(rows)


def _submenu_ts_style(t: dict) -> InlineKeyboardMarkup:
    """Sub-menu: visual/style settings."""
    tid = t["id"]
    layout = (t.get("bracket_layout") or "mirrored").lower()
    layout_lbl = "линейная" if layout == "linear" else "симметричная"
    overlay_alpha = int(t.get("bg_overlay_alpha") or 165)
    overlay_pct = int(round(overlay_alpha * 100 / 255))
    # ``row_bg_alpha`` is 0–255 like the overlay; mirror the same UX so
    # admins don't have to remember /set_row_alpha syntax.
    row_alpha = int(t.get("row_bg_alpha") or 255)
    row_pct = int(round(row_alpha * 100 / 255))
    gname = (t.get("group_display_name") or "").strip() or "Группа A"
    name_mode = (t.get("name_display_mode") or "full").lower()
    name_mode_lbl = {
        "full": "полные",
        "tag":  "только @теги",
        "nick": "только ники / команды",
    }.get(name_mode, "полные")
    rows = [
        [InlineKeyboardButton(
            f"🎨 Стиль сетки: {layout_lbl}",
            callback_data=f"ts:layout:{tid}",
        )],
        [InlineKeyboardButton(
            f"🌫 Затемнение фона: {overlay_pct}%",
            callback_data=f"ts:overlay:{tid}",
        )],
        [InlineKeyboardButton(
            f"🪟 Прозрачность строк: {row_pct}%",
            callback_data=f"ts:rowa:{tid}",
        )],
        [InlineKeyboardButton(
            f"🪪 Имена: {name_mode_lbl}",
            callback_data=f"ts:names:{tid}",
        )],
        [InlineKeyboardButton(
            f"🏷 Имя группы: {gname}",
            callback_data=f"ts:groupname:{tid}",
        )],
        [InlineKeyboardButton("⬅️ Назад к настройкам", callback_data=f"ts:open:{tid}")],
    ]
    return InlineKeyboardMarkup(rows)


def _submenu_ts_remind(t: dict) -> InlineKeyboardMarkup:
    """Sub-menu: reminder settings."""
    tid = t["id"]
    chat_rem = "вкл" if int(t.get("reminder_chat_enabled") or 0) else "выкл"
    dm_h = int(t.get("reminder_dm_hours") or 0)
    dm_lbl = "выкл" if dm_h <= 0 else f"{dm_h}ч"
    sig_min = int(t.get("signup_reminder_minutes") or 0)
    sig_lbl = "выкл" if sig_min <= 0 else f"{sig_min} мин"
    has_signup_link = bool((t.get("signup_link") or "").strip())
    has_signup_deadline = bool(t.get("signup_deadline_at"))
    link_lbl = "ссылка ✓" if has_signup_link else "без ссылки"
    deadline_lbl = "дедлайн ✓" if has_signup_deadline else "без дедлайна"
    rows = [
        [InlineKeyboardButton(
            f"🔔 DM-напоминания (по матчам): {dm_lbl}",
            callback_data=f"ts:dm:{tid}",
        )],
        [InlineKeyboardButton(
            f"💬 Чат-напоминания (по матчам): {chat_rem}",
            callback_data=f"ts:chat:{tid}",
        )],
        [InlineKeyboardButton(
            f"📣 Напоминалка о записи: {sig_lbl}",
            callback_data=f"ts:remsignup:{tid}",
        )],
        [InlineKeyboardButton(
            f"🔗 Ссылка на запись: {link_lbl}",
            callback_data=f"ts:remsignup_link:{tid}",
        )],
        [InlineKeyboardButton(
            f"📅 Дедлайн записи: {deadline_lbl}",
            callback_data=f"ts:remsignup_deadline:{tid}",
        )],
        [InlineKeyboardButton("⬅️ Назад к настройкам", callback_data=f"ts:open:{tid}")],
    ]
    return InlineKeyboardMarkup(rows)


def _submenu_ts_tours(t: dict) -> InlineKeyboardMarkup:
    """Sub-menu: tour (round) settings."""
    tid = t["id"]
    enabled = "вкл" if int(t.get("tours_enabled") or 0) else "выкл"
    cur = int(t.get("current_tour") or 0)
    total = int(t.get("total_tours") or 0)
    auto = "вкл" if int(t.get("auto_next_tour") or 0) else "выкл"
    rows = [
        [InlineKeyboardButton(
            f"📅 Режим туров: {enabled}",
            callback_data=f"ts:tours_toggle:{tid}",
        )],
        [InlineKeyboardButton(
            f"🔢 Всего туров: {total if total else 'авто'}",
            callback_data=f"ts:tours_total:{tid}",
        )],
        [InlineKeyboardButton(
            f"▶️ Текущий тур: {cur}",
            callback_data=f"ts:tours_setcur:{tid}",
        )],
        [InlineKeyboardButton(
            f"🤖 Авто-след.тур: {auto}",
            callback_data=f"ts:tours_auto:{tid}",
        )],
        [InlineKeyboardButton(
            "⚡ Создать матчи следующего тура",
            callback_data=f"ts:tours_next:{tid}",
        )],
        [InlineKeyboardButton("⬅️ Назад к настройкам", callback_data=f"ts:open:{tid}")],
    ]
    return InlineKeyboardMarkup(rows)


def submenu_top() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏅 Топ ELO (общий)", callback_data="top:elo")],
        [InlineKeyboardButton("⚽ Топ ВСА",          callback_data="top:vsa"),
         InlineKeyboardButton("🎮 Топ РИ",          callback_data="top:ri")],
        [InlineKeyboardButton("🏆 Лидерборд турнира", callback_data="top:leaderboard")],
        [InlineKeyboardButton("⚽ Топ бомбардиров",  callback_data="top:scorers")],
    ])


def submenu_feedback() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐞 Баг / 💡 Идея", callback_data="feedback_start")],
        [InlineKeyboardButton("👮 Запросить админку", callback_data="request_admin")],
    ])


def submenu_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Забанить",     callback_data="a:ban"),
         InlineKeyboardButton("✅ Разбанить",     callback_data="a:unban")],
        [InlineKeyboardButton("📋 Список банов", callback_data="a:banned")],
        [InlineKeyboardButton("⚖️ Изменить ELO (±)",   callback_data="a:elo"),
         InlineKeyboardButton("🎯 Задать ELO",         callback_data="a:setelo")],
        [InlineKeyboardButton("⚠️ Тех. поражение",      callback_data="a:walkover")],
        [InlineKeyboardButton("💾 Скачать БД",   callback_data="db:export"),
         InlineKeyboardButton("📥 Загрузить БД", callback_data="db:import")],
        [InlineKeyboardButton("📦 Скачать код бота", callback_data="db:export_bot")],
    ])


def _wizard_set(ctx: ContextTypes.DEFAULT_TYPE, step: str, data: dict | None = None):
    ctx.user_data["wizard"] = {"step": step, "data": data or {}}


def _wizard_clear(ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("wizard", None)


def _wizard_get(ctx: ContextTypes.DEFAULT_TYPE) -> dict | None:
    return ctx.user_data.get("wizard")


def _bump_album_counter(ctx, message) -> int | None:
    """
    For a photo posted as part of a Telegram album (`media_group_id` set),
    return the 1-based position of this photo within the album from the
    perspective of THIS handler invocation. Each call advances the
    counter for that media_group. Returns ``None`` for standalone photos.

    Telegram delivers each photo of an album as a separate update; they
    share the same `media_group_id`. We use ``ctx.chat_data`` to count
    how many we've seen so far. The counter persists for ~1 hour
    (cleaned up implicitly when chat_data is GC'd) which is way more
    than the few seconds an album takes to deliver.
    """
    mgi = getattr(message, "media_group_id", None)
    if not mgi:
        return None
    counters = ctx.chat_data.setdefault("_album_counters", {})
    counters[mgi] = counters.get(mgi, 0) + 1
    return counters[mgi]


# ── Multi-photo album state (continuation screenshots) ──────────────────────
#
# Telegram albums where a user uploads several scoreboard screenshots of
# the *same* match (because the goal list overflows one screen). Each
# photo arrives as its own update; we keep a tiny per-album state in
# ``ctx.chat_data["_album_state"]`` so the second / third photo can be
# recognised as a continuation of the first and have its goal list merged
# into the existing match instead of creating a duplicate.
#
# Lives in chat_data — a media_group survives for ~1 hour which is more
# than enough; the stale entries are GC'd with chat_data eventually.

def _album_state(ctx, mgi: str | None) -> dict | None:
    """Return the dict stashed for this album, creating an empty entry
    on first read. Returns ``None`` for standalone (non-album) photos."""
    if not mgi:
        return None
    states = ctx.chat_data.setdefault("_album_state", {})
    return states.setdefault(str(mgi), {})


def _album_lock(ctx, mgi: str) -> asyncio.Lock:
    """Return a per-album asyncio.Lock stored in chat_data.

    Guarantees that only one photo from the same album can execute the
    'first send' branch of ``_album_panel_send_or_edit`` at a time,
    preventing the race condition where two photos arrive in parallel,
    both see ``panel_msg_id is None``, and both call ``send_message``
    — producing two separate panel messages instead of one.
    """
    locks = ctx.chat_data.setdefault("_album_locks", {})
    if mgi not in locks:
        locks[mgi] = asyncio.Lock()
    return locks[mgi]


def _norm_team(s: str | None) -> str:
    """Normalize a team / nickname string for fuzzy comparison."""
    return (s or "").strip().lower()


def _teams_match(a: tuple[str | None, str | None],
                 b: tuple[str | None, str | None]) -> bool:
    """True if two (team1, team2) pairs refer to the same match.

    Fuzzy because OCR readings vary slightly between screenshots of the
    same scoreboard (one frame might pick up a stray pixel that flips
    a single character). Either ordering of the pair is accepted —
    Telegram clients sometimes shuffle home/away rendering across
    photos in the same album.
    """
    from difflib import SequenceMatcher
    a1, a2 = _norm_team(a[0]), _norm_team(a[1])
    b1, b2 = _norm_team(b[0]), _norm_team(b[1])
    if not a1 or not a2 or not b1 or not b2:
        return False

    def near(x: str, y: str) -> bool:
        # OCR can crop / abbreviate team names ("Barcelona" → "Barca"),
        # so we tolerate a fairly loose ratio. Combine with the *score*
        # match in the caller — random teams with the same score are
        # unlikely to also fuzzy-match by name.
        return x == y or SequenceMatcher(None, x, y).ratio() >= 0.65

    return (near(a1, b1) and near(a2, b2)) or (near(a1, b2) and near(a2, b1))


def _goal_key(g: dict) -> tuple[str, str, str]:
    """Stable identity of a goal event for dedup across photos."""
    name = (g.get("name") or "").strip().lower()
    minute = str(g.get("minute") or "").strip()
    side = (g.get("side") or "").strip().lower()
    return (name, minute, side)


def _merge_goal_lists(
    existing: list[dict] | None,
    incoming: list[dict] | None,
) -> tuple[list[dict], int]:
    """Append every goal from ``incoming`` into ``existing`` unless it's
    already there (matched by ``_goal_key``). Returns ``(merged, added)``
    where ``added`` is the number of newly inserted entries.
    """
    out = list(existing or [])
    seen = {_goal_key(g) for g in out}
    added = 0
    for g in (incoming or []):
        if not isinstance(g, dict):
            continue
        k = _goal_key(g)
        if k in seen:
            continue
        out.append(dict(g))
        seen.add(k)
        added += 1
    return out, added


# ── Album panel rendering ───────────────────────────────────────────────
#
# When the user uploads several screenshots in a single Telegram album
# (media-group), we replace the per-photo "🔍 Распознаю …" + per-photo
# picker dance with a single combined "📷 Альбом" panel message that we
# *edit* as photos arrive. Each match becomes a numbered row; ambiguous
# opponents become inline-button rows below. End result: 1 album = 1
# panel message regardless of how many screens are in it.
def _album_panel_render_text(album_state: dict) -> str:
    title = "📷 <b>Альбом матчей</b>"
    tlabel = album_state.get("tournament_label")
    if tlabel:
        title += f" — <b>{html.escape(tlabel)}</b>"
    matches = album_state.get("matches") or []
    if not matches:
        return f"{title}\n\nОбрабатываю скрины…"
    lines = [title + ":"]
    for i, m in enumerate(matches, 1):
        s = m.get("status")
        if s == "submitted":
            if m.get("auto_confirmed"):
                lines.append(f"✅ {i}) {m.get('summary', '?')} — засчитан")
            else:
                lines.append(f"✓ {i}) {m.get('summary', '?')} — на проверке")
        elif s == "merged":
            target = m.get("merged_into", "?")
            added = m.get("added_goals", 0)
            if added:
                lines.append(
                    f"📎 {i}) +{added} гол(ов) в матч {target}"
                )
            else:
                lines.append(
                    f"📎 {i}) скрин #{target} (продолжение, новых голов нет)"
                )
        elif s == "ambiguous":
            lines.append(
                f"⚠️ {i}) {m.get('summary', '?')} — выбери соперника:"
            )
        elif s == "skipped":
            lines.append(f"⏭ {i}) {m.get('summary', '?')} — исключено")
        elif s == "error":
            # error_text is built by the producer with safe HTML (we
            # control the substitution and do not interpolate raw user
            # input). Keep the existing trust contract: html.escape was
            # historically applied here, but that double-escapes ``<b>``
            # tags from match summaries. Sanitise just the user-provided
            # bits (usernames in mention()) on the producer side, and
            # render the line as HTML so ``<b>2:0</b>`` shows correctly.
            err_line = f"✗ {i}) {m.get('error_text') or 'не распознан'}"
            lines.append(err_line)
            # Show /reocr hint for errors where the photo is available
            if m.get("file_id"):
                tid_hint = m.get("target_tid")
                tid_str = f" {tid_hint}" if tid_hint else ""
                u1 = f"@{m['p1_username']}" if m.get("p1_username") else "@player1"
                u2 = f"@{m['p2_username']}" if m.get("p2_username") else "@player2"
                lines.append(
                    f"  ↳ <code>/reocr {u1} {u2}{tid_str}</code>"
                )
        else:
            lines.append(f"… {i}) обработка")
    pending = sum(1 for m in matches if m.get("status") == "ambiguous")
    submitted = sum(1 for m in matches if m.get("status") == "submitted")
    auto_confirmed = sum(1 for m in matches if m.get("status") == "submitted" and m.get("auto_confirmed"))
    awaiting_admin = submitted - auto_confirmed
    if pending == 0 and submitted:
        parts = []
        if auto_confirmed:
            parts.append(f"{auto_confirmed} засчитан(о)")
        if awaiting_admin:
            parts.append(f"{awaiting_admin} на проверке у админа")
        lines.append(f"\n📨 Готово: {', '.join(parts)}.")
    elif pending:
        lines.append(
            f"\n⏳ Жду уточнения по {pending} матч(ам)."
        )
    # ── Model signature: show which OCR model was used ──
    models_used: set[str] = set()
    for m in matches:
        mdl = m.get("ocr_model")
        if mdl:
            models_used.add(mdl)
    if models_used:
        models_str = ", ".join(sorted(models_used))
        lines.append(f"\n🤖 <i>OCR: {html.escape(models_str)}</i>")
    return "\n".join(lines)


def _album_panel_keyboard(mgi: str, album_state: dict):
    """Inline-keyboard for the album panel: rows of opponent-pick buttons
    for each ambiguous match, plus a cancel row when applicable."""
    matches = album_state.get("matches") or []
    rows = []
    for i, m in enumerate(matches):
        if m.get("status") != "ambiguous":
            continue
        cands = m.get("candidates") or []
        for c in cands[:4]:
            label = (
                f"№{i + 1}: @{c.get('username', '?')} "
                f"({int(round(c.get('ratio', 0) * 100))}%)"
            )
            rows.append([InlineKeyboardButton(
                label,
                callback_data=f"albmpick:{mgi}:{i}:{c['player_id']}",
            )])
        rows.append([InlineKeyboardButton(
            f"№{i + 1}: ⏭ Исключить",
            callback_data=f"albmskip:{mgi}:{i}",
        )])
    if any(m.get("status") == "ambiguous" for m in matches):
        rows.append([InlineKeyboardButton(
            "❌ Отменить весь альбом",
            callback_data=f"albmcancel:{mgi}",
        )])
    return InlineKeyboardMarkup(rows) if rows else None


async def _album_panel_send_or_edit(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    mgi: str,
    album_state: dict,
):
    """Send the album panel for the first time, or edit the existing one
    in-place to reflect new matches.

    Uses a per-album asyncio.Lock to prevent the race condition where two
    photos from the same album arrive concurrently, both see
    ``panel_msg_id is None``, and each calls ``send_message`` — resulting
    in two separate panel messages instead of one shared panel.
    """
    text = _album_panel_render_text(album_state)
    kb = _album_panel_keyboard(mgi, album_state)

    # Fast path: panel already exists — just edit it (no lock needed,
    # edit_message_text is idempotent and Telegram ignores identical edits).
    if album_state.get("panel_msg_id"):
        try:
            await ctx.bot.edit_message_text(
                chat_id=album_state.get("panel_chat_id", chat_id),
                message_id=album_state["panel_msg_id"],
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except TelegramError:
            # Common case: identical text+markup — Telegram returns
            # 'Bad Request: message is not modified'. Silent-ignore.
            pass
        return

    # Slow path: panel does not exist yet — acquire lock so only one
    # coroutine sends the initial message.
    lock = _album_lock(ctx, mgi)
    async with lock:
        # Re-check inside the lock: a sibling coroutine may have already
        # sent the panel while we were waiting.
        if album_state.get("panel_msg_id"):
            try:
                await ctx.bot.edit_message_text(
                    chat_id=album_state.get("panel_chat_id", chat_id),
                    message_id=album_state["panel_msg_id"],
                    text=_album_panel_render_text(album_state),
                    parse_mode="HTML",
                    reply_markup=_album_panel_keyboard(mgi, album_state),
                )
            except TelegramError:
                pass
            return
        try:
            m = await ctx.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            album_state["panel_msg_id"] = m.message_id
            album_state["panel_chat_id"] = chat_id
        except TelegramError as e:
            log.warning("album panel send failed: %s", e)


WELCOME_TEXT = (
    "👋 <b>FC Mobile League Bot</b>\n\n"
    "Выбирай действие на клавиатуре внизу.\n"
    "Все команды (например <code>/profile</code>, <code>/report</code>) тоже работают — "
    "так что можешь и ими.\n"
    "Подсказку по командам — <code>/help</code> или кнопка ℹ️."
)


async def cmd_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/version`` — show the running deploy's git commit and start time.

    Use this to confirm a freshly merged PR is actually the one running.
    """
    chat = update.effective_chat
    if chat is None:
        return
    commit = BUILD_INFO.get("commit") or "unknown"
    branch = BUILD_INFO.get("branch") or "unknown"
    msg = BUILD_INFO.get("message") or ""
    source = BUILD_INFO.get("source") or "unknown"
    lines = [
        "🔧 <b>Build info</b>",
        f"Commit: <code>{html.escape(str(commit))}</code>",
        f"Branch: <code>{html.escape(str(branch))}</code>",
    ]
    if msg:
        lines.append(f"Message: {html.escape(msg)}")
    lines.append(f"Source: <code>{html.escape(source)}</code>")
    lines.append(f"Started: <code>{BOT_STARTED_AT}</code>")
    lines.append(
        "\nProject: https://github.com/pavlodrab/elobot\n"
        f"Open commit: https://github.com/pavlodrab/elobot/commit/{commit}"
    )
    await ctx.bot.send_message(
        chat.id,
        "\n".join(lines),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _wizard_clear(ctx)
    user_id = update.effective_user.id
    if _is_group_chat(update):
        # In group chats Telegram's bottom keyboard is unavailable to us,
        # so attach an inline-button menu directly to this welcome message.
        await update.message.reply_text(
            WELCOME_TEXT,
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(user_id),
        )
        return
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=_menu_kb_for(update, user_id),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.effective_message
    if msg is None:
        return
    # The admin help text is well over Telegram's 4096-char limit, so always
    # split before sending; only the *last* chunk gets the menu keyboard.
    chunks = _split_for_telegram(help_text_for(user_id))
    last_idx = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        kb = _menu_markup_for(update, user_id) if i == last_idx else None
        try:
            await msg.reply_text(
                chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb,
            )
        except TelegramError as e:
            # If HTML parsing somehow still fails, fall back to plain text so
            # the user sees the help instead of the generic error message.
            log.warning("cmd_help reply_text failed (chunk %d/%d): %s — retrying without HTML",
                        i + 1, len(chunks), e)
            await msg.reply_text(
                chunk,
                disable_web_page_preview=True,
                reply_markup=kb,
            )
    # Attach the full command reference (COMMANDS.md) as a downloadable file.
    # The /help text only covers the most-used commands; COMMANDS.md is the
    # exhaustive list of every command and alias — handy for power users and
    # admins. Shipped with the repo, so it's always in sync with code.
    await _attach_commands_reference(msg)


# Path to the full command reference file shipped with the repo.
_COMMANDS_REFERENCE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "COMMANDS.md"
)


async def _attach_commands_reference(msg) -> None:
    """Send ``COMMANDS.md`` as a downloadable document alongside /help.

    Best-effort: any failure here (file missing, Telegram error, etc.) is
    logged but not surfaced — the user still sees the regular help text.
    """
    path = _COMMANDS_REFERENCE_PATH
    if not os.path.exists(path):
        log.info("_attach_commands_reference: %s not found, skipping", path)
        return
    try:
        with open(path, "rb") as fh:
            await msg.reply_document(
                document=fh,
                filename="COMMANDS.md",
                caption=(
                    "📚 <b>Полный справочник команд</b>\n"
                    "Все 84 команды и 201 алиас — для быстрого поиска."
                ),
                parse_mode="HTML",
            )
    except TelegramError as e:
        log.warning("_attach_commands_reference failed: %s", e)
    except OSError as e:
        log.warning("_attach_commands_reference OSError: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# /report  (with optional vsa|ri suffix)
# ─────────────────────────────────────────────────────────────────────────────

# ``SCORE_RE`` is now owned by :mod:`handlers.match`; re-imported above for
# backward compatibility (``from bot import SCORE_RE`` continues to work).


def _find_pending_playoff_leg(
    p1_id: int, p2_id: int, tid: int,
) -> dict | None:
    """Return the lowest-leg ``status='pending'`` playoff match between the
    two players in tournament ``tid``, or ``None`` if there's no truly
    empty leg waiting to be filled. Stages are walked in
    :data:`PLAYOFF_STAGES` order so the oldest open round wins ties.

    Used by the album-mode report flow to deterministically assign each
    concurrent photo to a distinct leg slot — without this, three photos
    of the same pair race against ``get_pending_match`` and may all
    update the same row (overwriting each other).
    """
    pair = (min(p1_id, p2_id), max(p1_id, p2_id))
    best = None
    # Walk every possible playoff stage — ``third`` is not in the linear
    # ``PLAYOFF_STAGES`` flow but a 3rd-place leg is still reportable.
    for stage in ALL_PLAYOFF_STAGES:
        rows = db.get_tournament_matches(tid, stage=stage)
        for m in rows:
            mpair = (
                min(m["player1_id"], m["player2_id"]),
                max(m["player1_id"], m["player2_id"]),
            )
            if mpair != pair:
                continue
            if m.get("status") != "pending":
                continue
            leg = int(m.get("leg") or 1)
            if best is None or leg < int(best.get("leg") or 1):
                best = m
        if best is not None:
            return best
    return None


def _next_playoff_leg_spec(
    p1_id: int, p2_id: int, tid: int,
) -> tuple[str, int] | None:
    """Determine the ``(stage, leg)`` tuple a brand-new playoff match
    between this pair should occupy.

    The latest playoff stage that already has *any* row between this pair
    is reused, with ``leg = max(existing_legs) + 1``. This is what
    :func:`tournament.advance_playoff` already does when an aggregate
    draw forces an extra leg; mirroring it here means a third album
    screenshot landing after L1/L2 produces a proper L3 (in the correct
    stage like ``sf``) instead of a generic ``stage='playoff'`` orphan
    that the bracket-walker ignores.

    Returns ``None`` if the pair has no existing playoff rows at all
    (caller should fall back to whatever stage logic it had before).
    """
    pair = (min(p1_id, p2_id), max(p1_id, p2_id))
    chosen_stage = None
    chosen_max_leg = 0
    # ``ALL_PLAYOFF_STAGES`` includes the 3rd-place fixture so an album
    # report for the bronze match lands on stage='third' instead of
    # being orphaned.
    for stage in ALL_PLAYOFF_STAGES:
        rows = db.get_tournament_matches(tid, stage=stage)
        max_leg_here = 0
        any_here = False
        for m in rows:
            mpair = (
                min(m["player1_id"], m["player2_id"]),
                max(m["player1_id"], m["player2_id"]),
            )
            if mpair != pair:
                continue
            any_here = True
            leg = int(m.get("leg") or 1)
            if leg > max_leg_here:
                max_leg_here = leg
        if any_here:
            chosen_stage = stage
            chosen_max_leg = max_leg_here
    if chosen_stage is None:
        return None
    return (chosen_stage, chosen_max_leg + 1)


async def _do_report(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    reporter: dict,
    opponent: dict,
    s1: int,
    s2: int,
    tournament_type: str | None = None,
    tournament: dict | None = None,
    stats_extra: dict | None = None,
    screenshot_hash: str | None = None,
    screenshot_file_id: str | None = None,
    ocr_goals: list[dict] | None = None,
    suppress_result_message: bool = False,
    force_new: bool = False,
) -> int | None:
    """Shared logic between /report and the photo handler.

    If `tournament` is supplied (e.g. resolved from the chat / caption tag in
    the photo handler) it overrides the per-player resolver — the result
    will land in that exact tournament regardless of the player's other
    active tournaments of the same type.

    When ``suppress_result_message=True`` the awaiting-admin "Результат
    матча …" confirmation is **not** posted to the chat — the caller is
    responsible for showing that text itself (e.g. by editing the inline
    keyboard message). The text is stashed on
    ``ctx.user_data['_last_report_result_text']`` so the caller can
    surface it without re-formatting. Used by the screenshot-OCR and
    ``pickrep:`` callbacks to keep the bot at exactly **one** message
    per reported match.

    Returns the resulting ``match_id`` (or ``None`` on early bailout)
    so the caller can wire up follow-up state — e.g. recording the match
    against an album-merge session.

    On every early bailout the reason is stashed on
    ``ctx.user_data['_last_report_error']`` (a short Russian phrase
    suitable for ``— …`` suffix in the album panel). Album/caller code
    pops this to surface the actual reason instead of a generic "не
    удалось записать". The slot is cleared on success.
    """
    def _bail(reason: str) -> None:
        # Helper to record the bailout reason for the album panel /
        # caller. Keep it concise — it gets shown right after the
        # match summary line.
        try:
            ctx.user_data["_last_report_error"] = reason
        except Exception:
            pass

    # Clear any stale slot from a previous call before we evaluate
    # this one — otherwise a successful report would still leave a
    # ghost reason hanging around.
    try:
        ctx.user_data.pop("_last_report_error", None)
    except Exception:
        pass

    if opponent["id"] == reporter["id"]:
        _bail("нельзя сыграть с самим собой")
        await send(update, "❌ Нельзя сыграть с самим собой.")
        return None

    # Ban checks — neither side can play if banned.
    for who, p in (("ты", reporter), ("соперник", opponent)):
        if is_player_banned(p):
            until = p.get("banned_until")
            _bail(f"{who} в бане до {until}")
            await send(
                update,
                f"❌ Не могу засчитать матч: {who} ({mention(p['username'])}) "
                f"в бане до <b>{until}</b>.",
            )
            return None

    if tournament is not None:
        t = tournament
    else:
        t = resolve_tournament_for_player(reporter, tournament_type)
    tid = t["id"] if t else None

    if t and t.get("required_channel"):
        ok, msg = await check_required_channel(ctx, reporter.get("telegram_id"), t["required_channel"])
        if not ok:
            _bail(f"требуется подписка на {html.escape(str(t['required_channel']))}")
            await send(update, msg + f"\n(Турнир «{t['name']}» требует подписку на {t['required_channel']})")
            return None

    # ── Cross-group guard ─────────────────────────────────────────────
    # If the tournament is in the group stage and BOTH players are
    # registered in tournament_players but in DIFFERENT groups, refuse —
    # otherwise we'd silently record a phantom cross-group match that
    # never shows up in either group's standings.
    if tid and t and (t.get("stage") or "groups") == "groups":
        try:
            tp_rows = db.get_tournament_players(tid)
            groups_by_pid = {r["player_id"]: r.get("group_name") for r in tp_rows}
        except Exception:
            groups_by_pid = {}
        g1 = groups_by_pid.get(reporter["id"])
        g2 = groups_by_pid.get(opponent["id"])
        # Both must be IN the tournament; if either is missing,
        # surface that as the reason. "?" is the lobby placeholder
        # used before the draw — treat it as "in tournament but
        # without a group yet" and skip the same-group check.
        in_tournament_a = reporter["id"] in groups_by_pid
        in_tournament_b = opponent["id"] in groups_by_pid
        if not in_tournament_a or not in_tournament_b:
            who_missing = (
                opponent.get("username") or "?"
                if not in_tournament_b
                else reporter.get("username") or "?"
            )
            _bail(
                f"@{html.escape(who_missing)} "
                f"не записан в турнир «{html.escape(t['name'])}»"
            )
            await send(
                update,
                f"❌ @{who_missing} не записан в турнир "
                f"<b>{html.escape(t['name'])}</b>. "
                f"Сначала <code>/add_player</code>.",
            )
            return None
        if g1 and g2 and g1 != "?" and g2 != "?" and g1 != g2:
            _bail(
                f"игроки в разных группах "
                f"({html.escape(str(g1))} и {html.escape(str(g2))})"
            )
            await send(
                update,
                f"❌ {mention(reporter.get('username'))} и "
                f"{mention(opponent.get('username'))} в разных группах "
                f"(<b>{html.escape(g1)}</b> и <b>{html.escape(g2)}</b>) "
                f"турнира <b>{html.escape(t['name'])}</b>. Между разными "
                f"группами групповые матчи не играются.",
            )
            return None

    # ── Pick the row we're going to update ────────────────────────────────
    # Album mode + playoff is special: when several screenshots of the same
    # pair arrive together (L1=tie, L2=tie, L3=win), each photo must claim
    # a distinct leg slot. ``get_pending_match`` returns *any* pending or
    # reported row and races between concurrent photos would otherwise
    # cause them to overwrite each other or pick up the wrong leg.
    #
    # Strategy:
    #   1. Prefer the lowest-leg row that is still truly ``pending`` (i.e.
    #      not yet claimed by another photo in this album).
    #   2. If every pre-created leg is already claimed, fall through to
    #      the "create a new leg" branch with the correct stage + leg
    #      (handled below).
    existing = None
    t_stage_for_existing = (t.get("stage") or "groups") if t else "groups"
    if force_new and tid and t_stage_for_existing == "playoff":
        existing = _find_pending_playoff_leg(
            reporter["id"], opponent["id"], tid,
        )
    if existing is None:
        existing = get_pending_match(reporter["id"], opponent["id"], tid)
    if existing:
        # In album mode (force_new) skip the "already reported" duplicate
        # guard — concurrent album photos would otherwise reject each other.
        # But we STILL use the existing pending match (important for
        # playoffs where matches are pre-created with correct stage/leg).
        if not force_new and existing["reported_by"] == reporter["id"] and \
                existing.get("status") in ("reported", "awaiting_admin"):
            _bail("ты уже сообщил результат этого матча")
            await send(update, "⚠️ Ты уже сообщил результат этого матча. Ожидай проверки админа.")
            return None
        upd_kwargs = dict(
            score1=s1 if existing["player1_id"] == reporter["id"] else s2,
            score2=s2 if existing["player1_id"] == reporter["id"] else s1,
            status="reported",
            reported_by=reporter["id"],
            stats_extra=json.dumps(stats_extra) if stats_extra else None,
        )
        if screenshot_hash:
            upd_kwargs["screenshot_hash"] = screenshot_hash
        if screenshot_file_id:
            upd_kwargs["screenshot_file_id"] = screenshot_file_id
        update_match(existing["id"], **upd_kwargs)
        match_id = existing["id"]
    else:
        # Determine the correct match stage from the tournament.
        # Tournament uses "groups"; match table uses "group" (singular).
        t_stage = (t.get("stage") or "groups") if t else "groups"
        match_stage = "group" if t_stage == "groups" else t_stage

        # ── Guard: block group-stage matches when playoff has started ──
        if tid and t and t_stage != "groups":
            mpp = max(1, int(t.get("group_matches_per_pair") or 1))
            pair_count = count_group_matches_for_pair(
                reporter["id"], opponent["id"], tid,
            )
            if pair_count >= mpp:
                # Pair already played enough group matches and the
                # tournament moved to playoff — this match belongs to
                # the current playoff round, NOT the group.
                pass  # match_stage is already set to the playoff round
            # If pair hasn't reached group limit but playoff started,
            # still record into the current stage (playoff).

        # ── Guard: group stage closed (playoff_started or stage moved) ──
        if tid and t and match_stage == "group" and t.get("playoff_started"):
            _bail("групповой этап закрыт — плей-офф уже идёт")
            await send(
                update,
                "🔒 Групповой этап закрыт — новые групповые матчи "
                "не принимаются.\nИспользуй плей-офф.",
            )
            return None

        # ── Guard: limit group-stage matches per pair ──
        # Enforced even in album (force_new) mode — the pair cap is an
        # absolute limit, not a duplicate-report guard.
        if tid and t and match_stage == "group":
            mpp = max(1, int(t.get("group_matches_per_pair") or 1))
            pair_count = count_group_matches_for_pair(
                reporter["id"], opponent["id"], tid,
            )
            if pair_count >= mpp:
                _bail(
                    f"уже сыграно {pair_count}/{mpp} групповых матчей в этой паре"
                )
                await send(
                    update,
                    f"⚠️ Вы уже сыграли {pair_count} из {mpp} групповых "
                    f"матч(ей) между собой в этом турнире. Больше нельзя.",
                )
                return None

        # Check for an existing confirmed group-stage match between the
        # same players — overwrite old settled results instead of creating
        # a duplicate.  Only "confirmed" matches are overwritten; matches
        # still under review ("awaiting_admin") are left alone so that
        # album uploads with multiple different matches between the same
        # pair each create their own record.
        group_existing = (
            get_existing_group_match(reporter["id"], opponent["id"], tid)
            if tid and t and t_stage == "groups" and not force_new
            else None
        )
        if group_existing and group_existing.get("status") == "confirmed":
            upd_kwargs = dict(
                score1=s1 if group_existing["player1_id"] == reporter["id"] else s2,
                score2=s2 if group_existing["player1_id"] == reporter["id"] else s1,
                status="reported",
                reported_by=reporter["id"],
                played_at=None,
                stats_extra=json.dumps(stats_extra) if stats_extra else None,
            )
            if screenshot_hash:
                upd_kwargs["screenshot_hash"] = screenshot_hash
            if screenshot_file_id:
                upd_kwargs["screenshot_file_id"] = screenshot_file_id
            update_match(group_existing["id"], **upd_kwargs)
            match_id = group_existing["id"]
            log.info(
                "Overwriting group match %s (was %s) between players %s and %s",
                match_id, group_existing.get("status"),
                reporter["id"], opponent["id"],
            )
        else:
            from database import create_match
            deadline = (datetime.utcnow() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
            create_stage = match_stage
            create_leg = 1
            # When tournament is in playoff stage, "playoff" is not a real
            # bracket round — figure out the actual stage label (e.g. "sf")
            # and the next leg number based on existing rows between this
            # pair. Without this, a third album screenshot landing after
            # L1/L2 spawns a generic ``stage='playoff'`` orphan that
            # ``advance_playoff`` ignores.
            if tid and t and t_stage == "playoff":
                spec = _next_playoff_leg_spec(
                    reporter["id"], opponent["id"], tid,
                )
                if spec is not None:
                    create_stage, create_leg = spec
            match_id = create_match(
                tid, reporter["id"], opponent["id"],
                stage=create_stage, deadline=deadline, leg=create_leg,
            )
            upd_kwargs = dict(
                score1=s1,
                score2=s2,
                status="reported",
                reported_by=reporter["id"],
                stats_extra=json.dumps(stats_extra) if stats_extra else None,
            )
            if screenshot_hash:
                upd_kwargs["screenshot_hash"] = screenshot_hash
            if screenshot_file_id:
                upd_kwargs["screenshot_file_id"] = screenshot_file_id
            update_match(match_id, **upd_kwargs)
    if screenshot_hash:
        try:
            db.record_processed_screenshot(
                screenshot_hash,
                tid,
                str(update.effective_chat.id) if update.effective_chat else None,
                match_id,
                reporter["id"],
            )
        except Exception:
            log.exception("record_processed_screenshot failed")

    # Persist OCR goal events for top-scorer leaderboards. Reporter's match
    # is "home" side from their POV; we need to map this onto p1/p2 in the
    # match record, where player1_id is whoever was created first.
    if ocr_goals:
        try:
            mrow = db.get_match(match_id)
            if mrow:
                p1_obj = get_player_by_id(mrow["player1_id"])
                p2_obj = get_player_by_id(mrow["player2_id"])
                if p1_obj and p2_obj:
                    # Reporter sees themselves as "home" on the screenshot,
                    # but in the DB schema p1 may or may not be the reporter.
                    # Re-map sides accordingly so green=>reporter, blue=>opp.
                    if mrow["player1_id"] == reporter["id"]:
                        # Reporter is p1 — sides line up.
                        _persist_ocr_goals(match_id, p1_obj, p2_obj, ocr_goals)
                    else:
                        # Reporter is p2 — flip sides.
                        flipped = []
                        for g in ocr_goals:
                            gg = dict(g)
                            if gg.get("side") == "home":
                                gg["side"] = "away"
                            elif gg.get("side") == "away":
                                gg["side"] = "home"
                            flipped.append(gg)
                        _persist_ocr_goals(match_id, p1_obj, p2_obj, flipped)
        except Exception:
            log.exception("persist ocr goals failed (_do_report)")

    t_label = t_full_label(t) if t else "—"

    # ── Auto-confirm path ──────────────────────────────────────────────
    # When the tournament has `auto_confirm=1`, skip the opponent-button
    # confirmation entirely (mimics WEEKEND CUP H2H). Match is recorded
    # as confirmed immediately, ELO is applied, series counter ticks.
    if t and int(t.get("auto_confirm") or 0) == 1:
        update_match(match_id, status="confirmed")
        try:
            summary = apply_result(match_id)
        except Exception:
            log.exception("apply_result failed in auto-confirm path")
            summary = {}
        d1 = summary.get("delta1", 0)
        d2 = summary.get("delta2", 0)
        elo1_after = summary.get("elo1_after", "?")
        elo2_after = summary.get("elo2_after", "?")
        msg_lines = [
            f"⚽ <b>Матч засчитан</b> [{t_label}]",
            f"{mention(reporter['username'])} <b>{s1}:{s2}</b> "
            f"{mention(opponent['username'])}",
            f"📈 ELO: {mention(reporter['username'])}: <b>{elo1_after}</b> "
            f"({arrow(d1)}) · {mention(opponent['username'])}: <b>{elo2_after}</b> "
            f"({arrow(d2)})",
        ]
        series_line = _format_series_line(match_id)
        if series_line:
            msg_lines.append(series_line)
        if summary.get("advanced_stage"):
            stage = summary["advanced_stage"]
            if stage == "finished":
                msg_lines.append("🏆 <b>Турнир завершён!</b>")
            else:
                stage_names = {"sf": "Полуфинал", "final": "Финал",
                               "qf": "Четвертьфинал", "r16": "1/8 финала",
                               "r32": "1/16 финала", "r64": "1/32 финала",
                               "r128": "1/64 финала", "r256": "1/128 финала",
                               "r512": "1/256 финала"}
                msg_lines.append(
                    f"🚀 Начинается <b>{stage_names.get(stage, stage.upper())}</b>!"
                )
        # Append custom footer text if configured
        from handlers.common import get_random_footer
        _footer = get_random_footer(t)
        if _footer:
            msg_lines.append(_footer)
        auto_text = "\n".join(msg_lines)
        # Same one-message contract as the awaiting-admin branch: stash so
        # the OCR/pickrep caller can surface this via ``edit_message_text``
        # instead of getting two messages from the bot.
        if ctx is not None and getattr(ctx, "user_data", None) is not None:
            ctx.user_data["_last_report_result_text"] = auto_text
        if not suppress_result_message:
            await send(update, auto_text)

        if opponent.get("telegram_id"):
            try:
                await ctx.bot.send_message(
                    opponent["telegram_id"],
                    f"⚽ Матч засчитан [{t_label}]\n"
                    f"{mention(reporter['username'])} <b>{s1}:{s2}</b> "
                    f"{mention(opponent['username'])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        if t and t.get("id"):
            await _announce_stage_advance(
                ctx, int(t["id"]), summary.get("advanced_stage")
            )
        return match_id

    # Skip opponent confirmation entirely — match goes straight to admin
    # review, since admins always re-check screenshots anyway. (The legacy
    # /confirm and confirm:* / dispute:* callbacks still work for old
    # `reported`-status rows that may exist in the DB.)
    update_match(match_id, status="awaiting_admin")
    fresh = get_match(match_id) or {}
    delivered = await _send_match_to_admins(ctx, dict(fresh)) if fresh else 0

    result_text = (
        f"⚽ <b>Результат матча</b> [{t_label}]\n\n"
        f"{mention(reporter['username'])} <b>{s1}:{s2}</b> {mention(opponent['username'])}\n\n"
        f"🛂 Отправлено админу на проверку. ELO начислится после согласия."
    )
    if not delivered:
        result_text += "\n\n⚠️ Админы не настроены — подойди к организатору вручную."
    # Append custom footer text if configured
    from handlers.common import get_random_footer
    _footer_txt = get_random_footer(t)
    if _footer_txt:
        result_text += _footer_txt
    # Stash the formatted text so callers using ``suppress_result_message``
    # can surface it themselves (e.g. via ``edit_message_text`` on the
    # inline-keyboard picker) without duplicating format strings.
    if ctx is not None and getattr(ctx, "user_data", None) is not None:
        ctx.user_data["_last_report_result_text"] = result_text
    if not suppress_result_message:
        await send(update, result_text)

    if opponent.get("telegram_id"):
        try:
            await ctx.bot.send_message(
                opponent["telegram_id"],
                f"📨 <b>{mention(reporter['username'])}</b> сообщил результат [{t_label}]:\n\n"
                f"{mention(reporter['username'])} <b>{s1}:{s2}</b> {mention(opponent['username'])}\n\n"
                f"🛂 Матч отправлен админу на проверку.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    return match_id


# ── Caption-based match report shortcut ──────────────────────────────────────
# Parses patterns like "@user1 3:2 @user2", "3:2 @opponent",
# "@opponent 3:2" from a photo caption. Allows players to bypass OCR
# entirely by typing the result under the screenshot.
_RE_CAPTION_REPORT = re.compile(
    r"@(\w+)\s+(\d{1,2}):(\d{1,2})\s+@(\w+)"  # @user1 3:2 @user2
    r"|(\d{1,2}):(\d{1,2})\s+@(\w+)"           # 3:2 @opponent
    r"|@(\w+)\s+(\d{1,2}):(\d{1,2})",          # @opponent 3:2
    re.IGNORECASE,
)

# Lighter pattern: just @username(s) without score — OCR will still be
# used for the score, but opponent is taken from the caption instead of
# fuzzy-matching the OCR'd nickname.
_RE_CAPTION_OPPONENT = re.compile(r"@(\w{2,})", re.IGNORECASE)


def _parse_caption_report(caption: str) -> tuple[int, int, str] | None:
    """Parse a score + opponent from a photo caption.

    Supports three forms:
      - "@user1 3:2 @user2" → (3, 2, "user2") — reporter is user1, opp is user2
      - "3:2 @opponent"     → (3, 2, "opponent")
      - "@opponent 2:1"     → (2, 1, "opponent") — score is reporter's perspective

    Returns (score1, score2, opponent_username) or None if no match.
    The scores are from the reporter's perspective (reporter scored score1).
    """
    if not caption:
        return None
    m = _RE_CAPTION_REPORT.search(caption)
    if not m:
        return None
    # Form 1: @user1 3:2 @user2
    if m.group(1) is not None:
        s1, s2 = int(m.group(2)), int(m.group(3))
        opp = m.group(4).lower()
        return s1, s2, opp
    # Form 2: 3:2 @opponent
    if m.group(5) is not None:
        s1, s2 = int(m.group(5)), int(m.group(6))
        opp = m.group(7).lower()
        return s1, s2, opp
    # Form 3: @opponent 3:2
    if m.group(8) is not None:
        opp = m.group(8).lower()
        s1, s2 = int(m.group(9)), int(m.group(10))
        return s1, s2, opp
    return None


def _parse_caption_opponent_only(caption: str) -> str | None:
    """Extract a single @username from caption (no score).

    Used when the caption has @mentions but no score — the bot will still
    OCR the screenshot for the score, but use the caption @username as the
    opponent instead of fuzzy-matching the OCR'd nickname.

    Returns the lowercased username (without @) or None.
    """
    if not caption:
        return None
    # Skip if the full form (with score) already matched — that path
    # handles everything.
    if _RE_CAPTION_REPORT.search(caption):
        return None
    mentions = _RE_CAPTION_OPPONENT.findall(caption)
    if not mentions:
        return None
    # Return the last @mention found (the first might be the reporter
    # themselves or a tournament name token).
    return mentions[-1].lower()


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    reporter = _player_from_user(user)
    if not reporter:
        await send(update, "❌ Сначала зарегистрируйся: /register")
        return

    if len(ctx.args) < 2:
        await send(update, "Использование: <code>/report 3:2 @opponent [вса|ри]</code>")
        return

    score_str = ctx.args[0]
    opp_arg = ctx.args[1]
    if not opp_arg.startswith("@") or len(opp_arg) < 2:
        await send(
            update,
            "❌ Соперника обязательно указывать через <b>@username</b>.\n"
            "Пример: <code>/report 3:2 @opponent</code>",
        )
        return
    opp_raw = opp_arg.lstrip("@").lower()
    t_type = parse_tournament_type_arg(ctx.args[2]) if len(ctx.args) > 2 else None

    m = SCORE_RE.match(score_str)
    if not m:
        await send(update, "❌ Неверный формат счёта. Пример: <code>3:2</code>")
        return

    s1, s2 = int(m.group(1)), int(m.group(2))
    if s1 > 30 or s2 > 30:
        await send(update, "❌ Слишком большой счёт. Максимум 30 голов.")
        return

    opponent = get_player(opp_raw)
    if not opponent:
        await send(update, f"❌ Игрок {mention(opp_raw)} не найден. Пусть зарегистрируется: /register")
        return

    # If we can't infer the tournament from the chat binding and the user is
    # in more than one active tournament, ask explicitly. Otherwise fall
    # through and let _do_report's resolver pick something sensible (the
    # single active tournament, or by type for legacy /report …
    # 3:2 @opp вса invocations).
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    chat_bound_t = get_tournament_by_chat(chat_id) if chat_id else None
    if chat_bound_t is None:
        eligible = _user_active_tournaments(reporter["id"])
        if t_type:  # explicit type narrows the picker
            eligible = [t for t in eligible if t.get("tournament_type") == t_type]
        if len(eligible) > 1:
            ctx.user_data["pending_report"] = {
                "s1": s1, "s2": s2,
                "opp_id": opponent["id"],
                "t_type": t_type,
            }
            kb = _tournament_picker_kb(eligible, "pickrep")
            await send(
                update,
                f"К какому турниру отнести матч "
                f"{mention(reporter['username'])} <b>{s1}:{s2}</b> "
                f"{mention(opponent['username'])}?",
                reply_markup=kb,
            )
            return

    await _do_report(
        update, ctx,
        reporter=reporter, opponent=opponent,
        s1=s1, s2=s2, tournament_type=t_type,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tournament resolution from a Telegram message (chat / caption)
# ─────────────────────────────────────────────────────────────────────────────

# Caption forms we accept as an explicit tournament reference.
# Examples:
#   "#турнир 27", "#турнир27", "#тур 27", "#tournament 27", "тур 27", "#27"
_RE_CAPTION_ID = re.compile(
    r"(?:#\s*)?(?:турнир|тур|tournament|tour)\s*#?\s*(\d+)",
    re.IGNORECASE,
)
_RE_CAPTION_BARE_ID = re.compile(r"#(\d+)\b")
_RE_TOKEN = re.compile(r"\w{5,}", re.UNICODE)

# Minimum tournament-name length eligible for free-form substring matching
# in a caption. Short names (1–2 chars, e.g. "О", "AB") are too noisy:
# they collide with stop-words, conjunctions, and OCR garbage. Such
# tournaments must be addressed by explicit ``#турнир <ID>`` reference.
_CAPTION_NAME_MIN_LEN = 3


def _find_tournament_by_caption_id(caption: str) -> dict | None:
    """If `caption` contains an explicit #турнир ID / #N reference, return it."""
    if not caption:
        return None
    m = _RE_CAPTION_ID.search(caption)
    if not m:
        m = _RE_CAPTION_BARE_ID.search(caption)
    if not m:
        return None
    try:
        tid = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return get_tournament(tid)


def _find_tournament_by_caption_name(caption: str) -> dict | None:
    """
    Match the caption against ACTIVE tournament names. Picks the
    longest unambiguous match. Returns None if nothing meaningful matches.

    Matching rules (stricter than a raw substring to avoid false positives
    where a single Russian letter "О" or a 2-char tag inside random text
    silently picked up an unrelated tournament):

    * Tournament names shorter than ``_CAPTION_NAME_MIN_LEN`` characters
      are ignored entirely — address them with ``#турнир <ID>``.
    * For longer names, require a regex-level word-boundary match so the
      name isn't found inside another word. Both the full name and its
      individual ≥5-char tokens are tried.
    """
    if not caption:
        return None
    cap = caption.lower()
    candidates: list[tuple[dict, int]] = []
    for t in get_active_tournaments():
        name_l = (t.get("name") or "").lower().strip()
        if not name_l:
            continue
        # Short names are not eligible for substring matching at all —
        # they're addressed by `#турнир <ID>` reference instead.
        if len(name_l) < _CAPTION_NAME_MIN_LEN:
            continue
        # Whole-name word-boundary match is the strongest signal.
        if _word_boundary_search(name_l, cap):
            candidates.append((t, len(name_l)))
            continue
        # Otherwise, check each "word" of the name (≥5 chars to avoid noise).
        for tok in _RE_TOKEN.findall(name_l):
            if tok and _word_boundary_search(tok, cap):
                candidates.append((t, len(tok)))
                break
    if not candidates:
        return None
    # Longest-match wins; ties → first (most recent).
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def _word_boundary_search(needle: str, haystack: str) -> bool:
    """True if ``needle`` appears in ``haystack`` bounded by non-word
    characters (or string edges). Both are expected to be already-lower
    case strings. Empty needle is never a match.
    """
    if not needle:
        return False
    try:
        pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
        return re.search(pattern, haystack, re.UNICODE) is not None
    except re.error:
        return needle in haystack


def resolve_tournament_for_photo(
    chat_id, caption: str, *, allow_chat_binding: bool = True,
) -> dict | None:
    """
    Decide which tournament a screenshot belongs to.

    Resolution order:
    1. Caption with explicit ID (e.g. ``#турнир 27`` / ``#27``) — wins everything.
    2. Caption substring match against an active tournament name.
    3. Chat binding (``tournaments.chat_id``) — *only* when
       ``allow_chat_binding`` is True. The photo handler turns this off
       in **group/channel** chats so unrelated screenshots aren't
       silently OCR'd just because the chat happens to be bound to a
       tournament; the caller must reference the tournament by name or
       ID. DM still uses the binding for convenience.
    4. Otherwise — None (caller should silently drop the photo).
    """
    cap = (caption or "").strip()

    t = _find_tournament_by_caption_id(cap)
    if t:
        return t

    t = _find_tournament_by_caption_name(cap)
    if t:
        return t

    if allow_chat_binding and chat_id is not None:
        t = get_tournament_by_chat(chat_id)
        if t:
            return t

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Photo handler — auto-OCR match screenshot
# ─────────────────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Channel posts have ``effective_user is None`` because there's no
    # authoring user behind a channel message — just a channel acting
    # as a sender. Silently drop them so the bot doesn't blast
    # "❌ Сначала зарегистрируйся" at posts forwarded from a linked
    # channel into the discussion group.
    if user is None:
        return

    # PTB routes ``filters.PHOTO`` not only to plain user messages but also
    # to ``edited_message`` / ``channel_post`` / ``business_message`` /
    # ``edited_channel_post``. Those updates have ``update.message is None``
    # and would otherwise crash on ``update.message.photo``. Pick the first
    # message-shaped attribute and bail out cleanly if none is present.
    msg = (
        update.message
        or update.edited_message
        or update.channel_post
        or update.edited_channel_post
        or getattr(update, "business_message", None)
    )
    if msg is None or not getattr(msg, "photo", None):
        return

    # ── Cache album photos by media_group_id ────────────────────────────
    # Telegram delivers album photos as separate updates sharing a single
    # media_group_id. We unconditionally stash each photo's file_id in
    # chat_data so that /admin_photo can later retrieve ALL photos of an
    # album even if auto-OCR skipped them (e.g. group chat without
    # tournament caption).
    _mg_id = getattr(msg, "media_group_id", None)
    if _mg_id and ctx.chat_data is not None:
        _mg_cache = ctx.chat_data.setdefault("_mg_photos", {})
        _mg_entry = _mg_cache.setdefault(str(_mg_id), [])
        _best = msg.photo[-1]
        if not any(e[0] == _best.file_id for e in _mg_entry):
            _mg_entry.append((_best.file_id, msg.message_id))

    # If the user is in feedback mode, treat the photo as a feedback attachment
    # rather than a match screenshot.
    if ctx.user_data.get("awaiting_feedback"):
        ctx.user_data.pop("awaiting_feedback", None)
        photos = msg.photo
        photo_file_id = photos[-1].file_id if photos else None
        text = msg.caption or ""
        if not ADMIN_IDS:
            await send(update, "❌ Админы не настроены — некому отправить фидбек.")
            return
        delivered = await _send_feedback_to_admins(ctx, user, text, photo_file_id=photo_file_id)
        if delivered:
            await send(update, f"✅ Спасибо! Доставлено {delivered}/{len(ADMIN_IDS)} админ(ам).")
        else:
            await send(update, "⚠️ Не удалось доставить ни одному админу.")
        return

    # Bug-report mode (sister flow to awaiting_feedback) — same plumbing,
    # different header so admins can triage by 🐞.
    if ctx.user_data.get("awaiting_bug"):
        ctx.user_data.pop("awaiting_bug", None)
        photos = msg.photo
        photo_file_id = photos[-1].file_id if photos else None
        text = msg.caption or ""
        if not ADMIN_IDS:
            await send(update, "❌ Админы не настроены — некому отправить отчёт.")
            return
        from handlers.leaderboard import _send_bug_to_admins
        delivered = await _send_bug_to_admins(
            ctx, user, text, photo_file_id=photo_file_id,
        )
        if delivered:
            await send(
                update,
                f"✅ Багрепорт отправлен ({delivered}/{len(ADMIN_IDS)} "
                f"админ(ам)). Спасибо!",
            )
        else:
            await send(update, "⚠️ Не удалось доставить ни одному админу.")
        return

    photos = msg.photo
    if not photos:
        return

    chat = update.effective_chat
    caption = (msg.caption or "").strip()

    # If the caption is a slash-command we own (e.g. /set_tournament_bg),
    # let the dedicated CommandHandler handle it instead of running OCR.
    if caption.startswith("/"):
        first = caption.split()[0].lower().lstrip("/").split("@", 1)[0]
        if first in {"set_tournament_bg", "set_bg", "tournament_bg"}:
            return

    # ── Tournament gate ──────────────────────────────────────────────────────
    # Resolve which tournament this screenshot belongs to from the chat
    # binding and/or the caption.
    #
    #  - In **groups/channels**: if we can't resolve a tournament, silently
    #    skip. The user asked us not to answer random screenshots that
    #    don't reference a specific tournament (no OCR, no error reply).
    #  - In **private chats (DM)**: there's no chat binding by design, so
    #    we fall back to the legacy OCR flow — try to detect the tournament
    #    type (ВСА/РИ) from the screenshot or caption and ask the user via
    #    buttons if it's ambiguous.
    chat_id = chat.id if chat else None
    chat_is_group = bool(chat and chat.type in ("group", "supergroup", "channel"))
    # In groups we DON'T fall back to the chat-binding — admin asked for
    # photos to be processed only when the caption explicitly references
    # the tournament (by name or by ``#ID``). Without that signal the
    # photo is silently ignored so random screenshots in the chat don't
    # trigger OCR.
    target_tournament = resolve_tournament_for_photo(
        chat_id, caption,
        allow_chat_binding=not chat_is_group,
    )
    # In Telegram albums only the first photo carries the caption.
    # Subsequent photos have an empty caption and would fail tournament
    # resolution in group chats.  Fall back to the tournament stashed by
    # the first album photo so the entire album is processed together.
    #
    # Race condition: with concurrent_updates the captionless photo may
    # start processing BEFORE the captioned photo has stashed the
    # tournament_id.  We retry a few times with a short sleep to give
    # the first photo time to write the value.
    album_mgi_gate = getattr(msg, "media_group_id", None)
    if target_tournament is None and album_mgi_gate:
        for _retry in range(6):
            prev_album = ctx.chat_data.get("_album_state", {}).get(
                str(album_mgi_gate), {},
            )
            stashed_tid = prev_album.get("tournament_id")
            if stashed_tid:
                target_tournament = get_tournament(stashed_tid)
                break
            await asyncio.sleep(0.5)
    # Stash the tournament in album state so later photos can use it.
    if target_tournament is not None and album_mgi_gate:
        ast = ctx.chat_data.setdefault("_album_state", {}).setdefault(
            str(album_mgi_gate), {},
        )
        ast.setdefault("tournament_id", target_tournament["id"])
    if target_tournament is None:
        if chat_is_group:
            log.info(
                "Photo skipped (no tournament reference in caption). "
                "chat_id=%s caption=%r",
                chat_id, caption[:80],
            )
            return
        # DM fallback — let the OCR flow figure it out.
        log.info(
            "Photo in DM, no explicit tournament — falling back to OCR-based detection. caption=%r",
            caption[:80],
        )

        # Before burning OCR quota, see if the user is in multiple active
        # tournaments. If so, ask which one this screenshot belongs to. The
        # in-screen "league plate" is intentionally NOT used as a hint.
        reporter_for_pick = _player_from_user(user)
        if reporter_for_pick:
            eligible_for_pick = _user_active_tournaments(reporter_for_pick["id"])
            if len(eligible_for_pick) > 1:
                photos_for_pick = msg.photo
                best_for_pick = photos_for_pick[-1] if photos_for_pick else None
                if best_for_pick:
                    ctx.user_data["pending_photo"] = {
                        "file_id": best_for_pick.file_id,
                    }
                    kb_pick = _tournament_picker_kb(
                        eligible_for_pick, "pickphoto"
                    )
                    await msg.reply_text(
                        "📸 К какому турниру отнести этот скрин?",
                        reply_markup=kb_pick,
                    )
                    return

    reporter = _player_from_user(user)
    if not reporter:
        # Only respond if the user explicitly addressed a tournament — that
        # way random photos in groups don't trigger any reply at all.
        await send(update, "❌ Сначала зарегистрируйся: /register, потом /setnick &lt;ник в игре&gt;")
        return
    if not reporter.get("game_nickname"):
        await send(
            update,
            "❌ Чтобы я мог понять, что это твой матч, укажи свой игровой ник: "
            "<code>/setnick TwoiNick</code>",
        )
        return
    if is_player_banned(reporter):
        await send(
            update,
            f"❌ Ты в бане до <b>{_fmt_dt(reporter['banned_until'])}</b>. "
            f"Причина: {reporter.get('banned_reason') or '—'}",
        )
        return

    # ── Caption shortcut: "@user1 3:2 @user2" or "3:2 @opponent" ────────────
    # If the caption contains an explicit score + opponent @username, skip
    # OCR entirely and record the match directly. This is the fallback when
    # AI models are exhausted — users can just type the result under the
    # screenshot.
    caption_match = _parse_caption_report(caption)
    if caption_match:
        c_s1, c_s2, c_opp_username = caption_match
        opponent = get_player(c_opp_username)
        if opponent and opponent["id"] != reporter["id"]:
            best = photos[-1]
            # Compute screenshot hash for dedup
            import hashlib, tempfile
            f = await ctx.bot.get_file(best.file_id)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                await f.download_to_drive(tmp_path)
                with open(tmp_path, "rb") as _fh:
                    screenshot_hash = hashlib.sha256(_fh.read()).hexdigest()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            t_type = (
                target_tournament["tournament_type"]
                if target_tournament else
                detect_tournament_type_from_caption(caption)
            )
            await _do_report(
                update, ctx,
                reporter=reporter,
                opponent=opponent,
                s1=c_s1,
                s2=c_s2,
                tournament_type=t_type,
                tournament=target_tournament,
                screenshot_hash=screenshot_hash,
                screenshot_file_id=best.file_id,
            )
            return
        elif not opponent:
            await send(
                update,
                f"❌ Игрок @{html.escape(c_opp_username)} не найден. "
                "Пусть зарегистрируется: /register",
            )
            return

    best = photos[-1]  # largest size
    await _process_match_photo(
        update, ctx,
        reporter=reporter,
        target_tournament=target_tournament,
        file_id=best.file_id,
        caption=caption,
    )


def _resolve_ocr_goal(raw_name: str, p1: dict, p2: dict, side: str | None
                       ) -> tuple[int | None, str | None]:
    """
    Map an OCR'd scorer name to one of the two players in the match.

    ``side`` (if not None) takes priority — it's derived from the team-strip
    colour (green = home = ``p1``, blue = away = ``p2``). Otherwise we
    fuzzy-compare the raw name against both players' game nicknames and pick
    the closer match (threshold 0.55).

    Returns ``(player_id, side)`` where ``side`` is the resolved side
    (``"home"``/``"away"``) so callers can store it.
    """
    raw = (raw_name or "").strip()
    if not raw:
        return None, side

    if side == "home":
        return p1["id"], "home"
    if side == "away":
        return p2["id"], "away"

    # No colour info — try to fuzzy-match against both players.
    from difflib import SequenceMatcher
    def _sim(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, canonical_nick(a), canonical_nick(b)).ratio()

    n1 = (p1.get("game_nickname") or p1.get("username") or "").strip()
    n2 = (p2.get("game_nickname") or p2.get("username") or "").strip()
    s1 = _sim(raw, n1)
    s2 = _sim(raw, n2)
    if max(s1, s2) < 0.55:
        return None, None
    if s1 >= s2:
        return p1["id"], "home"
    return p2["id"], "away"


# ── Scorer-name sanitizer (strips ГОЛ/GOAL suffix left by AI OCR) ────────────
import re as _re

_GOL_SUFFIX_RE = _re.compile(
    r"\s+(?:ГОЛ|GOAL|Гол|gol|Goal|GOL|гол)\.?\s*$",
    _re.IGNORECASE,
)
# Catch glued variants: "MbappéГОЛ", "DembéléGOAL"
_GOL_GLUED_RE = _re.compile(
    r"(?<=[a-zа-яёé])"  # preceded by a lowercase letter (name ending)
    r"(?:ГОЛ|GOAL|Гол|gol|GOL|гол)\.?\s*$",
    _re.IGNORECASE,
)


def _clean_raw_scorer_name(name: str) -> str:
    """Strip trailing 'ГОЛ'/'GOAL' suffix that AI OCR sometimes includes.

    Also removes common OCR artefacts like trailing dots, extra whitespace,
    and mis-reads of the 'ГОЛ' badge (same stop-words as Tesseract path).
    """
    if not name:
        return name
    s = name.strip()
    # Strip separated ГОЛ/GOAL suffix
    s = _GOL_SUFFIX_RE.sub("", s)
    # Strip glued ГОЛ/GOAL (no space between name and suffix)
    s = _GOL_GLUED_RE.sub("", s)
    # Strip trailing punctuation/whitespace artefacts
    s = s.rstrip(" .\t")
    return s


def _persist_ocr_goals(match_id: int, p1: dict, p2: dict,
                        ocr_goals: list[dict] | None) -> int:
    """
    Persist OCR-extracted goal events for the given match. Returns the
    number of goals stored. Silently no-ops when ``ocr_goals`` is empty.
    """
    if not ocr_goals:
        return 0
    rows: list[dict] = []
    for g in ocr_goals:
        if not isinstance(g, dict):
            continue
        name = _clean_raw_scorer_name((g.get("name") or "").strip())
        if not name:
            continue
        side = g.get("side")
        pid, side_resolved = _resolve_ocr_goal(name, p1, p2, side)
        rows.append({
            "player_id": pid,
            "raw_name":  name,
            "minute":    g.get("minute"),
            "side":      side_resolved,
        })
    if not rows:
        return 0

    # Validate side attribution against actual score. If the OCR
    # swapped green/blue, the home/away counts won't match the score.
    mrow = db.get_match(match_id)
    if mrow and mrow.get("score1") is not None and mrow.get("score2") is not None:
        s1, s2 = int(mrow["score1"]), int(mrow["score2"])
        home_count = sum(1 for r in rows if r["side"] == "home")
        away_count = sum(1 for r in rows if r["side"] == "away")
        if home_count == s2 and away_count == s1 and s1 != s2:
            # Clear swap: home goals match away score and vice versa.
            log.info("OCR sides swapped for match %s (%d home vs %d away, "
                     "score %d:%d) — flipping", match_id, home_count,
                     away_count, s1, s2)
            for r in rows:
                if r["side"] == "home":
                    r["side"] = "away"
                    r["player_id"] = p2["id"]
                elif r["side"] == "away":
                    r["side"] = "home"
                    r["player_id"] = p1["id"]
        elif (home_count != s1 or away_count != s2) and (home_count + away_count == s1 + s2):
            # Goal counts don't match the expected distribution but the
            # total is correct. This often happens on tied games (e.g. 2-2)
            # where the swap-detection above can't fire (s1 == s2). Also
            # catches partial misreads where e.g. one green ball was read
            # as blue.
            #
            # Strategy: if the reverse assignment (home_count == s2 and
            # away_count == s1) is correct, flip. Otherwise the counts
            # are just wrong (OCR artefact) — leave them as-is rather
            # than make things worse.
            if home_count == s2 and away_count == s1:
                log.info("OCR sides swapped for tied match %s (%d home vs %d away, "
                         "score %d:%d) — flipping", match_id, home_count,
                         away_count, s1, s2)
                for r in rows:
                    if r["side"] == "home":
                        r["side"] = "away"
                        r["player_id"] = p2["id"]
                    elif r["side"] == "away":
                        r["side"] = "home"
                        r["player_id"] = p1["id"]

    try:
        db.set_match_goals(match_id, rows)
    except Exception:
        log.exception("set_match_goals failed for match %s", match_id)
        return 0
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# «🔄 Другой моделью» retry helpers
#
# Each confirmation panel exposes a retry button that lets the user
# rotate to the next un-tried model in ``AI_FALLBACK_MODELS``. Telegram
# callback_data is capped at 64 bytes, so we hash the file_id to a
# 16-char token and stash the full retry context (file_id, list of
# already-tried models, caption, target_tournament_id, reporter_id)
# in ``ctx.user_data[f"ocr_retry_{token}"]``.
# ─────────────────────────────────────────────────────────────────────────────


def _retry_token_for(file_id: str) -> str:
    """16-char SHA-1 prefix of the Telegram file_id, fits in callback_data."""
    import hashlib
    return hashlib.sha1((file_id or "").encode("utf-8")).hexdigest()[:16]


def _retry_button_row(
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str,
    tried_models,
    target_tournament: dict | None,
    reporter_id: int | None,
    caption: str,
    panel_kind: str,
) -> list[InlineKeyboardButton]:
    """Build a one-button row that re-runs OCR with the next un-tried
    model, or an empty list if every model has already been shown.

    ``panel_kind`` is purely informational ("own"/"admin"/"ambig") —
    it lets the retry handler reproduce the right panel after the next
    OCR pass.
    """
    tried = list(tried_models or [])
    untried = [m for m in AI_FALLBACK_MODELS if m not in tried]
    if not untried:
        return []
    token = _retry_token_for(file_id)
    ctx.user_data[f"ocr_retry_{token}"] = {
        "file_id": file_id,
        "tried": tried,
        "caption": caption or "",
        "target_tournament_id": (
            target_tournament["id"] if target_tournament else None
        ),
        "reporter_id": reporter_id,
        "panel_kind": panel_kind,
    }
    pos = len(tried) + 1
    total = len(AI_FALLBACK_MODELS)
    return [
        InlineKeyboardButton(
            f"🔄 Другой моделью ({pos}/{total})",
            callback_data=f"retryocr:{token}",
        )
    ]


def _model_used_in(res) -> str | None:
    """Pick the AI model name that produced this MatchScreenshot, or
    None when tesseract was used."""
    raw = getattr(res, "raw_texts", None) or {}
    return raw.get("_ai_model") or None


async def _process_match_photo(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    reporter: dict,
    target_tournament: dict | None,
    file_id: str,
    caption: str = "",
    ai_models_override: tuple[str, ...] | list[str] | None = None,
    tried_models: tuple[str, ...] | list[str] = (),
):
    """
    Run OCR on a screenshot and present the user with a confirmation panel.

    Shared between the inline photo handler (where ``update.message`` is the
    photo itself), the ``pickphoto:`` callback (where ``update.message``
    is the picker reply), and the ``retryocr:`` retry button (where the
    callback originates from a previous confirmation panel).

    ``ai_models_override`` pins a specific model chain (one element on
    retry). ``tried_models`` is the cumulative list of models already
    shown to the user — used to compute «X из Y» in the retry button
    label and to skip already-tried models on subsequent retries.
    """
    is_retry = ai_models_override is not None
    msg = update.effective_message
    # Detect album mode early so we can pick the right UI affordance.
    # ``mgi`` (media-group-id) is set only for true Telegram albums
    # (2+ photos in one upload). The album panel replaces per-photo
    # "🔍 Распознаю …" preambles with a single shared message that we
    # edit as each photo is processed.
    album_mgi = (
        getattr(update.message, "media_group_id", None)
        if update.message else None
    )
    is_album = album_mgi is not None
    album_state_early = _album_state(ctx, album_mgi)
    if is_album and album_state_early is not None:
        if target_tournament is not None and not album_state_early.get(
            "tournament_label"
        ):
            album_state_early["tournament_label"] = (
                f"{target_tournament['name']} "
                f"[{t_full_label(target_tournament)}]"
            )
        # Send the "📷 Альбом — обрабатываю …" placeholder once per album.
        chat_id_for_panel = (
            update.effective_chat.id if update.effective_chat else None
        )
        if (
            chat_id_for_panel is not None
            and not album_state_early.get("panel_msg_id")
        ):
            await _album_panel_send_or_edit(
                ctx, chat_id_for_panel, str(album_mgi), album_state_early,
            )

    # Download photo to a temp file
    f = await ctx.bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await f.download_to_drive(tmp_path)

        # ── Duplicate screenshot detection ──────────────────────────────
        # Hash the bytes BEFORE running expensive OCR. If we've already
        # processed this exact image, reject silently with a numbered
        # "screenshot already recorded" reply (mimics WEEKEND CUP H2H).
        import hashlib
        with open(tmp_path, "rb") as _fh:
            screenshot_hash = hashlib.sha256(_fh.read()).hexdigest()
        target_tid = target_tournament["id"] if target_tournament else None
        already = (
            db.find_match_by_screenshot_hash(screenshot_hash, target_tid)
            or db.get_processed_screenshot(screenshot_hash, target_tid)
        )
        if already:
            # In an album we want each duplicate to be addressed by its
            # 1-based position in that album. Telegram doesn't expose the
            # ordinal directly, so we count how many photos sharing this
            # media_group_id we've seen so far in this chat.
            album_idx = (
                _bump_album_counter(ctx, update.message)
                if update.message else None
            )
            if is_album and album_state_early is not None:
                # Append a duplicate-row to the panel instead of sending
                # a separate "❌ Скриншот N …" message.
                matches = album_state_early.setdefault("matches", [])
                matches.append({
                    "status": "error",
                    "error_text": (
                        f"Скриншот {album_idx or len(matches) + 1}: "
                        "уже записан ранее"
                    ),
                })
                chat_id_dup = (
                    update.effective_chat.id
                    if update.effective_chat else None
                )
                if chat_id_dup is not None:
                    await _album_panel_send_or_edit(
                        ctx, chat_id_dup, str(album_mgi), album_state_early,
                    )
            elif album_idx is not None:
                await send(
                    update,
                    f"❌ Скриншот {album_idx}: Результат уже записан ранее",
                )
            else:
                await send(update, "❌ Этот скриншот уже был обработан ранее.")
            return
        if not is_album and not is_retry:
            # Album panel placeholder is enough — don't spam additional
            # "Распознаю …" messages per photo when we're in album mode.
            # On retry the callback handler has already edited the
            # original confirmation panel to "🔄 Перепознаю…", no need
            # to send a fresh one.
            if target_tournament is not None:
                await msg.reply_text(
                    f"🔍 Распознаю скрин матча для турнира "
                    f"<b>{target_tournament['name']}</b> "
                    f"[{t_full_label(target_tournament)}]…",
                    parse_mode="HTML",
                )
            else:
                await msg.reply_text("🔍 Распознаю скрин матча…")

        # ── Determine effective ai_models for this call ────────────────
        # In 'score_only' mode (tesseract-only), skip AI entirely by
        # passing ai_models=() which makes parse_match_screenshot jump
        # straight to the local tesseract pipeline — no network calls,
        # no model latency.
        # In 'ai_no_tess' mode, AI-only — tesseract fallback is disabled.
        _ocr_mode_early = (
            (target_tournament.get("ocr_mode") or "ai")
            if target_tournament else "ai"
        )
        _effective_ai_models = ai_models_override
        _no_tesseract = False
        if _ocr_mode_early == "score_only" and ai_models_override is None:
            _effective_ai_models = ()  # force tesseract-only
        elif _ocr_mode_early == "ai_no_tess":
            _no_tesseract = True  # AI only, no tesseract fallback

        try:
            res = await asyncio.to_thread(
                parse_match_screenshot,
                tmp_path, ai_models=_effective_ai_models,
                no_tesseract=_no_tesseract,
            )
        except Exception as e:
            log.exception("OCR failed: %s", e)
            await send(update, f"❌ Не смог распознать фото: <code>{e}</code>")
            return

        # NOTE: We deliberately do NOT use the in-screen "league plate"
        # text (e.g. "Лига Гвардиолыча") as a tournament resolver. That's
        # an in-game league name, completely unrelated to which Telegram
        # tournament the screenshot belongs to. Tournament resolution is
        # done up front via chat-binding / caption-tag only.

        # Tournament type. If the caller pinned a tournament — that wins.
        # In DM-fallback mode, derive it from caption first, then the OCR.
        if target_tournament is not None:
            t_type = target_tournament["tournament_type"]
        else:
            t_type = (
                detect_tournament_type_from_caption(caption)
                or getattr(res, "tournament_type", None)
            )

        # Score
        if res.score1 is None or res.score2 is None:
            if is_album and album_state_early is not None:
                matches = album_state_early.setdefault("matches", [])
                matches.append({
                    "status": "error",
                    "error_text": "счёт не распознан",
                    "file_id": file_id,
                    "target_tid": target_tournament["id"] if target_tournament else None,
                })
                chat_id_err = (
                    update.effective_chat.id
                    if update.effective_chat else None
                )
                if chat_id_err is not None:
                    await _album_panel_send_or_edit(
                        ctx, chat_id_err, str(album_mgi), album_state_early,
                    )
                return
            await send(
                update,
                "⚠️ Не смог распознать счёт на скрине. Попробуй ещё раз "
                "или используй <code>/report 3:2 @opponent</code>.\n\n"
                f"Что я увидел: «{res.raw_texts.get('score', '')}»",
            )
            return

        # ── Album continuation: same match, more goals on a 2nd screenshot ──
        # If this photo arrived as part of a Telegram album AND a previous
        # photo in the same album already produced a match (same teams +
        # same score), this photo is just a "scroll-down" of the goal
        # list. Merge the new scorers into the existing match instead of
        # creating a duplicate.
        mgi = (
            getattr(update.message, "media_group_id", None)
            if update.message else None
        )
        album_state = _album_state(ctx, mgi)
        new_goals_list = list(getattr(res, "goals", []) or [])
        if album_state and album_state.get("sig"):
            prev_sig = album_state["sig"]
            new_sig = (res.team1, res.team2, res.score1, res.score2)
            same_score = (
                prev_sig[2] == new_sig[2] and prev_sig[3] == new_sig[3]
            )
            same_teams = _teams_match(
                (prev_sig[0], prev_sig[1]),
                (new_sig[0], new_sig[1]),
            )
            # Check if goals overlap — if they don't, it's a different
            # match (same players, same score, different game).
            _existing_goals = album_state.get("goals") or []
            _has_overlap = False
            if _existing_goals and new_goals_list:
                _existing_keys = {_goal_key(g) for g in _existing_goals}
                _has_overlap = any(
                    _goal_key(g) in _existing_keys for g in new_goals_list
                )
            elif not _existing_goals or not new_goals_list:
                # One side has no goals — can't distinguish, assume continuation
                _has_overlap = True
            if same_score and same_teams and _has_overlap:
                merged, added = _merge_goal_lists(
                    _existing_goals, new_goals_list
                )
                album_state["goals"] = merged
                primary_match_id = album_state.get("match_id")
                if primary_match_id and added:
                    # The first photo's match is already in the DB —
                    # rewrite its goal list with the merged set so
                    # leaderboards reflect every scorer.
                    p1m = get_player_by_id(album_state.get("p1_id"))
                    p2m = get_player_by_id(album_state.get("p2_id"))
                    try:
                        if p1m and p2m:
                            _persist_ocr_goals(
                                primary_match_id, p1m, p2m, merged,
                            )
                    except Exception:
                        log.exception(
                            "album-merge: persist goals failed for "
                            "match %s", primary_match_id,
                        )
                else:
                    # First photo not confirmed yet — patch the merged
                    # goals into its ``ocr_extra_<file_id>`` stash so
                    # the eventual ``_do_report`` picks up everyone.
                    primary_fid = album_state.get("primary_file_id")
                    if primary_fid:
                        extra = ctx.user_data.get(
                            f"ocr_extra_{primary_fid}"
                        )
                        if isinstance(extra, dict):
                            extra["goals"] = list(merged)
                # Album panel: append a "📎 продолжение" row instead of
                # firing a separate chat message.
                if is_album and album_state_early is not None:
                    matches_panel = album_state_early.setdefault(
                        "matches", [],
                    )
                    # The "primary" match's display index is whichever
                    # entry in matches[] points to the same screenshot.
                    primary_idx_disp = None
                    for i, mm in enumerate(matches_panel, 1):
                        if mm.get("file_id") == album_state.get(
                            "primary_file_id"
                        ):
                            primary_idx_disp = i
                            break
                    matches_panel.append({
                        "status": "merged",
                        "merged_into": primary_idx_disp or 1,
                        "added_goals": added,
                        "file_id": file_id,
                    })
                    chat_id_merge = (
                        update.effective_chat.id
                        if update.effective_chat else None
                    )
                    if chat_id_merge is not None:
                        await _album_panel_send_or_edit(
                            ctx, chat_id_merge, str(album_mgi),
                            album_state_early,
                        )
                    return
                if added:
                    await send(
                        update,
                        f"📎 Это продолжение того же матча. Добавил "
                        f"<b>{added}</b> новых голов из этого скрина "
                        f"(всего: {len(merged)})."
                        + (
                            ""
                            if primary_match_id
                            else "\n<i>Подтвердишь матч с первого фото — "
                                 "голы пойдут вместе.</i>"
                        ),
                    )
                else:
                    await send(
                        update,
                        "📎 Это продолжение того же матча — новых голов "
                        "на этом скрине не нашлось.",
                    )
                return

        # Reporter is one side. Opponent is the other side.
        # Match team1/team2 against players' game_nicknames via fuzzy lookup.
        my_nick = reporter["game_nickname"]
        side1 = res.team1 or ""
        side2 = res.team2 or ""

        # Fuzzy: which side is "me"?
        from difflib import SequenceMatcher
        def sim(a, b):
            if not a or not b:
                return 0.0
            return SequenceMatcher(None, canonical_nick(a), canonical_nick(b)).ratio()

        sim1 = sim(my_nick, side1)
        sim2 = sim(my_nick, side2)
        my_is_side1 = sim1 >= sim2
        my_score = res.score1 if my_is_side1 else res.score2
        opp_score = res.score2 if my_is_side1 else res.score1
        opp_text = side2 if my_is_side1 else side1

        # ── Score-only OCR mode ─────────────────────────────────────────
        # When ocr_mode='score_only', the bot extracts only the score from
        # the screenshot and does NOT try to identify opponents by nickname.
        # The user must specify the opponent via caption (@username) or the
        # bot asks them to do so.
        _ocr_mode = (
            (target_tournament.get("ocr_mode") or "ai")
            if target_tournament else "ai"
        )
        if _ocr_mode == "score_only":
            # Check if user provided @opponent in the caption
            caption_opp_username = _parse_caption_opponent_only(caption)
            caption_opp = None
            if caption_opp_username:
                caption_opp = get_player(caption_opp_username)
                if caption_opp and caption_opp["id"] == reporter["id"]:
                    caption_opp = None  # can't play against yourself

            if target_tournament is not None:
                tid = target_tournament["id"]
                t_label = (
                    f"{target_tournament['name']} (ID {tid}, "
                    f"{t_full_label(target_tournament)})"
                )
            else:
                tid = None
                t_label = (
                    t_type_label(t_type) if t_type else "❓ турнир не распознан"
                )

            # Stash OCR extras for later confirmation
            ctx.user_data[f"ocr_extra_{file_id}"] = {
                "league_plate": res.league_plate,
                "raw": res.raw_texts,
                "screenshot_hash": screenshot_hash,
                "goals": list(getattr(res, "goals", []) or []),
                "mgi": mgi,
                "team1": res.team1,
                "team2": res.team2,
                "score1": res.score1,
                "score2": res.score2,
            }

            if caption_opp:
                # Opponent from caption — show confirmation
                tid_tag = f":{tid}" if tid is not None else ""
                own_tried = list(tried_models)
                own_used = _model_used_in(res)
                if own_used and own_used not in own_tried:
                    own_tried.append(own_used)
                own_retry = _retry_button_row(
                    ctx,
                    file_id=file_id,
                    tried_models=own_tried,
                    target_tournament=target_tournament,
                    reporter_id=reporter["id"] if reporter else None,
                    caption=caption,
                    panel_kind="own",
                )
                own_rows = [
                    [
                        InlineKeyboardButton(
                            "✅ Всё верно — отправить",
                            callback_data=(
                                f"ocr_pick:{caption_opp['id']}:{my_score}:{opp_score}:{t_type}"
                                f":{target_tournament['id'] if target_tournament else '0'}"
                                f":{_retry_token_for(file_id)}"
                            ),
                        ),
                        InlineKeyboardButton("❌ Отмена", callback_data="ocr_cancel"),
                    ]
                ]
                if own_retry:
                    own_rows.append(own_retry)
                kb = InlineKeyboardMarkup(own_rows)
                await msg.reply_text(
                    f"📋 <b>Ручная OCR (tesseract)</b>\n"
                    f"  Счёт: <b>{my_score}:{opp_score}</b>\n"
                    f"  Турнир: <b>{html.escape(t_label)}</b>\n\n"
                    f"👤 Соперник (из подписи): {mention(caption_opp['username'])}\n\n"
                    f"Подтверди отправку результата.",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                # No opponent specified — ask user
                tid_hint = f" {tid}" if tid else ""
                await msg.reply_text(
                    f"📋 <b>Ручная OCR (tesseract)</b>\n"
                    f"  Счёт: <b>{my_score}:{opp_score}</b>\n"
                    f"  Турнир: <b>{html.escape(t_label)}</b>\n\n"
                    f"⚠️ Укажи соперника — отправь скрин с подписью "
                    f"<code>@username</code>, или введи вручную:\n"
                    f"<code>/report {my_score}:{opp_score} @opponent</code>",
                    parse_mode="HTML",
                )
            return

        # Reporter must actually appear in the match. If neither side on
        # the screenshot looks anything like the reporter's game nickname
        # (e.g. ratio < 0.55 on the closer side), refuse — it's somebody
        # else's screenshot and registering it would corrupt their stats.
        # ADMINS bypass this check: they're allowed to register matches
        # between two other players (e.g. for technical reasons).
        best_self_sim = max(sim1, sim2)
        reporter_is_admin = is_admin(update.effective_user.id)
        if best_self_sim < 0.55 and not reporter_is_admin:
            saw_side1 = html.escape(side1 or "?")
            saw_side2 = html.escape(side2 or "?")
            my_nick_safe = html.escape(my_nick)
            # Hand off to admins so they see the actual screenshot and
            # can decide whether to /admin_report it manually. This is
            # the path that fired for the "Loading…" / "tw..hLUCS" case.
            try:
                await _send_failed_screenshot_to_admins(
                    ctx,
                    file_id=file_id,
                    score1=res.score1,
                    score2=res.score2,
                    p1_username=None,
                    p2_username=None,
                    tournament=target_tournament,
                    reporter_user=update.effective_user,
                    reason="не твой матч (никнейм репортёра не совпал)",
                    extra_note=(
                        f"Ник репортёра: «{my_nick}», "
                        f"OCR ники на скрине: «{side1 or '?'}» / «{side2 or '?'}»"
                    ),
                )
            except Exception:
                log.exception("not-my-match handoff")
            if is_album and album_state_early is not None:
                matches_a = album_state_early.setdefault("matches", [])
                matches_a.append({
                    "status": "error",
                    "error_text": (
                        f"не твой матч (ник «{my_nick_safe}», "
                        f"на скрине «{saw_side1}» / «{saw_side2}»)"
                    ),
                    "file_id": file_id,
                    "ocr_model": _model_used_in(res) or "tesseract",
                    "target_tid": target_tournament["id"] if target_tournament else None,
                })
                chat_id_err2 = (
                    update.effective_chat.id
                    if update.effective_chat else None
                )
                if chat_id_err2 is not None:
                    await _album_panel_send_or_edit(
                        ctx, chat_id_err2, str(album_mgi),
                        album_state_early,
                    )
                return
            await send(
                update,
                "🚫 Это не твой матч.\n"
                f"Твой игровой ник: <b>{my_nick_safe}</b>, а на скрине играют "
                f"<b>{saw_side1}</b> и <b>{saw_side2}</b>.\n\n"
                "Если это всё-таки твой матч, проверь правильность ника командой "
                "<code>/setnick TwoiNick</code>. Регистрировать чужие матчи "
                "нельзя — каждый игрок присылает скрин сам.",
            )
            return

        # Admin proxy-report: when an admin sends a screenshot they're not
        # in, we treat side1 / side2 as the actual participants. The match
        # gets recorded as side1 vs side2 (not as "admin vs side2"). This
        # mimics /admin_report but with auto-extracted nicknames + score.
        admin_proxy = reporter_is_admin and best_self_sim < 0.55
        if admin_proxy:
            cands1 = find_players_by_fuzzy_game_nickname(side1 or "")
            cands2 = find_players_by_fuzzy_game_nickname(side2 or "")
            # Drop pairwise duplicates (a player shouldn't be both sides)
            top1 = cands1[0] if cands1 else None
            top2 = cands2[0] if cands2 else None
            if top1 and top2 and top1[0]["id"] == top2[0]["id"]:
                # Same player on both sides — drop the worse fit.
                if top1[1] >= top2[1]:
                    top2 = cands2[1] if len(cands2) > 1 else None
                else:
                    top1 = cands1[1] if len(cands1) > 1 else None

            if not top1 or not top2 or top1[1] < 0.55 or top2[1] < 0.55:
                s1_safe = html.escape(side1 or "?")
                s2_safe = html.escape(side2 or "?")
                t_arg_hint = (
                    f" {target_tournament['id']}" if target_tournament else ""
                )
                # Hand off to admins: the screenshot itself + the parsed
                # nicknames so they can manually create the match with
                # /admin_report. Best-effort — don't block the panel.
                try:
                    await _send_failed_screenshot_to_admins(
                        ctx,
                        file_id=file_id,
                        score1=res.score1,
                        score2=res.score2,
                        p1_username=None,
                        p2_username=None,
                        tournament=target_tournament,
                        reporter_user=update.effective_user,
                        reason="админ-репорт: не нашёл игроков по OCR",
                        extra_note=(
                            f"OCR-ники: «{side1 or '?'}» / «{side2 or '?'}»"
                        ),
                    )
                except Exception:
                    log.exception("admin-proxy fail handoff")
                if is_album and album_state_early is not None:
                    matches_a = album_state_early.setdefault("matches", [])
                    matches_a.append({
                        "status": "error",
                        "error_text": (
                            f"админ-репорт: не найдены игроки "
                            f"«{s1_safe}» / «{s2_safe}»"
                        ),
                        "file_id": file_id,
                        "ocr_model": _model_used_in(res) or "tesseract",
                        "target_tid": target_tournament["id"] if target_tournament else None,
                    })
                    cid_ae = (
                        update.effective_chat.id
                        if update.effective_chat else None
                    )
                    if cid_ae is not None:
                        await _album_panel_send_or_edit(
                            ctx, cid_ae, str(album_mgi),
                            album_state_early,
                        )
                    return
                await send(
                    update,
                    "⚠️ Это админ-репорт чужого матча, но не получилось уверенно "
                    f"найти обоих игроков по нику со скрина (<b>{s1_safe}</b> / <b>{s2_safe}</b>).\n\n"
                    "Внеси вручную: "
                    f"<code>/admin_report @user1 @user2 {res.score1}:{res.score2}{t_arg_hint}</code>",
                )
                return

            p1, r1 = top1
            p2, r2 = top2

            # ── Album mode: auto-confirm admin-proxy match ──────────
            if is_album and album_state_early is not None:
                matches_a = album_state_early.setdefault("matches", [])
                try:
                    mid_apx = await _do_report(
                        update, ctx,
                        reporter=p1,
                        opponent=p2,
                        s1=int(res.score1),
                        s2=int(res.score2),
                        tournament_type=t_type,
                        tournament=target_tournament,
                        screenshot_hash=screenshot_hash,
                        screenshot_file_id=file_id,
                        ocr_goals=list(
                            getattr(res, "goals", []) or []
                        ),
                        suppress_result_message=True,
                        force_new=True,
                    )
                except Exception as e:
                    log.exception("album admin-proxy _do_report failed: %s", e)
                    mid_apx = None
                ctx.user_data.pop("_last_report_result_text", None)
                short_apx = (
                    f"👮 {mention(p1['username'])} "
                    f"<b>{res.score1}:{res.score2}</b> "
                    f"{mention(p2['username'])}"
                )
                _apx_model = _model_used_in(res) or "tesseract"
                if mid_apx:
                    matches_a.append({
                        "status": "submitted",
                        "summary": short_apx,
                        "match_id": mid_apx,
                        "file_id": file_id,
                        "ocr_model": _apx_model,
                        "auto_confirmed": bool(
                            target_tournament
                            and int(target_tournament.get("auto_confirm") or 0) == 1
                        ),
                    })
                else:
                    # Pull the bailout reason from the helper's last
                    # call so the panel surfaces e.g. "уже сыграно 1/1
                    # групповых матчей в этой паре" instead of the
                    # generic "не удалось записать". Cleared by
                    # _do_report on the next call so it doesn't leak
                    # across album entries.
                    bail_reason = (
                        ctx.user_data.pop("_last_report_error", None)
                        or "не удалось записать"
                    )
                    # Forward the screenshot to admins with the actual
                    # reason — they can manually /admin_report or fix
                    # whatever guard tripped (e.g. add the player to the
                    # tournament). Best-effort: don't block the panel.
                    try:
                        await _send_failed_screenshot_to_admins(
                            ctx,
                            file_id=file_id,
                            score1=res.score1,
                            score2=res.score2,
                            p1_username=p1.get("username"),
                            p2_username=p2.get("username"),
                            tournament=target_tournament,
                            reporter_user=update.effective_user,
                            reason=bail_reason,
                        )
                    except Exception:
                        log.exception("admin-proxy bail handoff")
                    # ``short_apx`` carries literal HTML (``<b>...</b>``)
                    # for the score, but the album panel runs the whole
                    # error_text through ``html.escape``, which turns
                    # those tags into visible ``<b>`` text. Strip the
                    # HTML markup here so the rendered line stays clean.
                    plain_apx = (
                        f"👮 @{p1.get('username') or '?'} "
                        f"{res.score1}:{res.score2} "
                        f"@{p2.get('username') or '?'}"
                    )
                    matches_a.append({
                        "status": "error",
                        "error_text": f"{plain_apx} — {bail_reason}",
                        "file_id": file_id,
                        "ocr_model": _apx_model,
                        "target_tid": target_tournament["id"] if target_tournament else None,
                        "p1_username": p1.get("username") or None,
                        "p2_username": p2.get("username") or None,
                    })
                cid_apx_album = (
                    update.effective_chat.id
                    if update.effective_chat else None
                )
                if cid_apx_album is not None:
                    await _album_panel_send_or_edit(
                        ctx, cid_apx_album, str(album_mgi),
                        album_state_early,
                    )
                return

            tid_apx = target_tournament["id"] if target_tournament else None
            tid_apx_tag = f":{tid_apx}" if tid_apx is not None else ""
            t_label_apx = (
                f"{target_tournament['name']} (ID {tid_apx}, "
                f"{t_full_label(target_tournament)})"
                if target_tournament else (t_type_label(t_type) if t_type else "—")
            )
            apx_tried = list(tried_models)
            apx_used = _model_used_in(res)
            if apx_used and apx_used not in apx_tried:
                apx_tried.append(apx_used)
            apx_retry_row = _retry_button_row(
                ctx,
                file_id=file_id,
                tried_models=apx_tried,
                target_tournament=target_tournament,
                reporter_id=reporter["id"] if reporter else None,
                caption=caption,
                panel_kind="admin",
            )
            apx_rows = [
                [InlineKeyboardButton(
                    "✅ Записать матч (админ)",
                    callback_data=(
                        f"apxconfirm:{p1['id']}:{p2['id']}:"
                        f"{res.score1}:{res.score2}{tid_apx_tag}"
                    ),
                )],
            ]
            if apx_retry_row:
                apx_rows.append(apx_retry_row)
            apx_rows.append(
                [InlineKeyboardButton("❌ Отмена", callback_data="ocr_cancel")]
            )
            kb = InlineKeyboardMarkup(apx_rows)
            league_line = (res.league_plate or "").splitlines()[-1].strip() if (res.league_plate or "").strip() else ""
            league_line = (league_line[:60] + "…") if len(league_line) > 60 else (league_line or "—")
            ai_model = res.raw_texts.get("_ai_model") if res.raw_texts else None
            await msg.reply_text(
                "👮 <b>Админ-режим</b> — ты не участник этого матча, "
                "записываю как чужой.\n\n"
                f"📋 Распознано:\n"
                f"  Счёт: <b>{res.score1}:{res.score2}</b>\n"
                f"  Игрок 1: {mention(p1['username'])} "
                f"(<i>{html.escape(p1.get('game_nickname','—'))}</i>, "
                f"совпадение {int(r1*100)}%)\n"
                f"  Игрок 2: {mention(p2['username'])} "
                f"(<i>{html.escape(p2.get('game_nickname','—'))}</i>, "
                f"совпадение {int(r2*100)}%)\n"
                f"  Турнир: <b>{html.escape(t_label_apx)}</b>\n"
                f"  Лига на скрине: <i>{html.escape(league_line)}</i>"
                + (f"\n  <i>OCR: {html.escape(ai_model)}</i>" if ai_model else ""),
                parse_mode="HTML",
                reply_markup=kb,
            )
            ctx.user_data[f"ocr_extra_{file_id}"] = {
                "league_plate": res.league_plate,
                "raw": res.raw_texts,
                "screenshot_hash": screenshot_hash,
                "goals": list(getattr(res, "goals", []) or []),
            }
            return

        # Fuzzy look up opponent in DB.
        # ── Caption override: if the user tagged @opponent in the caption,
        # use that directly instead of fuzzy-matching the OCR'd nickname.
        # This handles the case where OCR garbles the name but the user
        # helpfully wrote @username under the screenshot.
        caption_opp_username = _parse_caption_opponent_only(caption)
        if caption_opp_username:
            caption_opp_player = get_player(caption_opp_username)
            if caption_opp_player and caption_opp_player["id"] != reporter["id"]:
                # Direct hit from caption — skip fuzzy entirely.
                candidates = [(caption_opp_player, 1.0)]
            else:
                candidates = find_players_by_fuzzy_game_nickname(opp_text)
        else:
            candidates = find_players_by_fuzzy_game_nickname(opp_text)
        # Drop self from candidates (rare edge case)
        candidates = [c for c in candidates if c[0]["id"] != reporter["id"]]

        # Build a confirmation panel. If a tournament was pinned (chat
        # binding / caption), show its full label including ID. Otherwise
        # (DM fallback) show the OCR-detected type or a placeholder.
        if target_tournament is not None:
            tid = target_tournament["id"]
            t_label = (
                f"{target_tournament['name']} (ID {tid}, "
                f"{t_full_label(target_tournament)})"
            )
        else:
            tid = None
            t_label = (
                t_type_label(t_type) if t_type else "❓ турнир не распознан"
            )

        # All OCR-derived strings MUST be HTML-escaped before being sent
        # back via parse_mode="HTML" — Tesseract regularly produces stray
        # smart quotes ("/"/«/»/”), `<`, `>` and unbalanced glyphs that
        # crash Telegram's HTML entity parser. The league plate especially
        # is mostly noise.
        league_line = (res.league_plate or "").splitlines()[-1].strip() if (res.league_plate or "").strip() else ""
        # Trim noisy plate to first 60 chars so a long block of OCR garbage
        # doesn't blow up the message.
        league_line = (league_line[:60] + "…") if len(league_line) > 60 else (league_line or "—")
        debug = (
            f"📋 Распознано:\n"
            f"  Счёт: <b>{my_score}:{opp_score}</b>\n"
            f"  Ты: <b>{html.escape(my_nick)}</b> "
            f"↔ {html.escape(repr(side1))} / {html.escape(repr(side2))}\n"
            f"  Соперник на скрине: <b>{html.escape(opp_text) or '?'}</b>\n"
            f"  Турнир: <b>{html.escape(t_label)}</b>\n"
            f"  Лига на скрине: <i>{html.escape(league_line)}</i>"
        )
        ai_model = res.raw_texts.get("_ai_model") if res.raw_texts else None
        if ai_model:
            debug += f"\n  <i>OCR: {html.escape(ai_model)}</i>"

        # In the DM fallback path, if neither chat binding nor caption gave
        # us a tournament AND the OCR couldn't tell the type, ask the user
        # which tournament type the screenshot belongs to.
        if target_tournament is None and not t_type:
            ctx.user_data["ocr_pending"] = {
                "file_id": file_id,
                "my_score": my_score,
                "opp_score": opp_score,
                "opp_text": opp_text or "",
            }
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("ВСА", callback_data="ocr_tt:vsa"),
                    InlineKeyboardButton("РИ",  callback_data="ocr_tt:ri"),
                ]
            ])
            await msg.reply_text(
                debug + "\n\nК какому турниру отнести?",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # The "tid" path tag — empty when no tournament was pinned (so the
        # callback doesn't get a misleading tail field).
        tid_tag = f":{tid}" if tid is not None else ""

        # ─────────── Album mode: route through shared panel ───────────
        # In album uploads we don't want N separate picker messages — we
        # have a single "📷 Альбом" panel that we keep editing. Each
        # photo becomes a row; unambiguous opponents are auto-confirmed,
        # ambiguous ones surface inline buttons in the panel.
        if is_album and album_state is not None:
            chat_id_album = (
                update.effective_chat.id
                if update.effective_chat else None
            )
            matches = album_state.setdefault("matches", [])
            # Stash this photo's OCR data — needed by the album-pick
            # callback when the user later picks an opponent.
            ctx.user_data[f"ocr_extra_{file_id}"] = {
                "league_plate": res.league_plate,
                "raw": res.raw_texts,
                "screenshot_hash": screenshot_hash,
                "goals": list(getattr(res, "goals", []) or []),
                "mgi": mgi,
                "team1": res.team1,
                "team2": res.team2,
                "score1": res.score1,
                "score2": res.score2,
            }
            # Make this photo the album's "primary" if it's the first
            # one with successful OCR (so later continuation photos can
            # merge into it).
            if album_state.get("sig") is None:
                album_state["sig"] = (
                    res.team1, res.team2, res.score1, res.score2,
                )
                album_state["goals"] = list(
                    getattr(res, "goals", []) or []
                )
                album_state["primary_file_id"] = file_id

            opp_safe = html.escape(opp_text or "?")
            short_summary = (
                f"{mention(reporter['username'])} "
                f"<b>{my_score}:{opp_score}</b> «{opp_safe}»"
            )
            unambig = (
                len(candidates) == 1
                or (
                    len(candidates) > 1
                    and (candidates[0][1] - candidates[1][1]) >= 0.1
                )
            )
            if not candidates:
                matches.append({
                    "status": "error",
                    "error_text": (
                        f"в базе нет игрока, похожего на «{opp_safe}»"
                    ),
                    "file_id": file_id,
                    "ocr_model": _model_used_in(res) or "tesseract",
                })
            elif not unambig:
                # Ambiguous — surface buttons in the panel.
                cands_serial = [
                    {
                        "player_id": p["id"],
                        "username": p.get("username", ""),
                        "game_nickname": p.get("game_nickname", ""),
                        "ratio": float(ratio),
                    }
                    for (p, ratio) in candidates[:4]
                ]
                matches.append({
                    "status": "ambiguous",
                    "summary": short_summary,
                    "candidates": cands_serial,
                    "file_id": file_id,
                    "my_score": my_score,
                    "opp_score": opp_score,
                    "t_type": t_type,
                    "target_tid": tid,
                    "ocr_model": _model_used_in(res) or "tesseract",
                })
            else:
                opponent = candidates[0][0]
                # Auto-confirm: skip the picker, write the match
                # straight away (admin still has to approve).
                _t_label_album = (
                    f"{target_tournament['name']} (ID {tid}, "
                    f"{t_full_label(target_tournament)})"
                    if target_tournament else (
                        t_type_label(t_type) if t_type else "—"
                    )
                )
                try:
                    match_id_done = await _do_report(
                        update, ctx,
                        reporter=reporter,
                        opponent=opponent,
                        s1=int(my_score),
                        s2=int(opp_score),
                        tournament_type=t_type,
                        tournament=target_tournament,
                        screenshot_hash=screenshot_hash,
                        screenshot_file_id=file_id,
                        ocr_goals=list(
                            getattr(res, "goals", []) or []
                        ),
                        suppress_result_message=True,
                        force_new=True,
                    )
                except Exception as e:
                    log.exception("album auto-confirm failed: %s", e)
                    match_id_done = None
                # Drop the temporary stashed result text — album panel
                # owns the user-visible summary now.
                ctx.user_data.pop("_last_report_result_text", None)
                if match_id_done:
                    matches.append({
                        "status": "submitted",
                        "summary": (
                            f"{mention(reporter['username'])} "
                            f"<b>{my_score}:{opp_score}</b> "
                            f"{mention(opponent['username'])}"
                        ),
                        "match_id": match_id_done,
                        "file_id": file_id,
                        "ocr_model": _model_used_in(res) or "tesseract",
                        "auto_confirmed": bool(
                            target_tournament
                            and int(target_tournament.get("auto_confirm") or 0) == 1
                        ),
                    })
                    # Stamp album-state so downstream continuation
                    # photos (same teams+score) merge into THIS match
                    # instead of creating a duplicate.
                    if album_state.get("primary_file_id") == file_id:
                        album_state["match_id"] = match_id_done
                        album_state["p1_id"] = reporter["id"]
                        album_state["p2_id"] = opponent["id"]
                else:
                    bail_reason_2 = (
                        ctx.user_data.pop("_last_report_error", None)
                        or "не удалось записать"
                    )
                    # Hand off to admins so they can still salvage the
                    # match manually (e.g. cross-group / pair-cap tripped).
                    try:
                        await _send_failed_screenshot_to_admins(
                            ctx,
                            file_id=file_id,
                            score1=res.score1,
                            score2=res.score2,
                            p1_username=(
                                reporter.get("username") if reporter else None
                            ),
                            p2_username=(
                                opponent.get("username") if opponent else None
                            ),
                            tournament=target_tournament,
                            reporter_user=update.effective_user,
                            reason=bail_reason_2,
                        )
                    except Exception:
                        log.exception("self-report bail handoff")
                    matches.append({
                        "status": "error",
                        "error_text": f"{short_summary} — {bail_reason_2}",
                        "file_id": file_id,
                        "ocr_model": _model_used_in(res) or "tesseract",
                        "target_tid": target_tournament["id"] if target_tournament else None,
                    })

            if chat_id_album is not None:
                await _album_panel_send_or_edit(
                    ctx, chat_id_album, str(album_mgi), album_state,
                )
            return

        if not candidates:
            opp_safe = html.escape(opp_text)
            t_type_safe = html.escape(t_type or "")
            await send(
                update,
                debug + "\n\n"
                f"❌ В базе нет игрока с ником, похожим на «{opp_safe}».\n"
                f"Попроси соперника зарегистрироваться и указать <code>/setnick {opp_safe}</code>,\n"
                f"или используй вручную: <code>/report {my_score}:{opp_score} @opponent {t_type_safe}</code>",
            )
            return

        own_tried = list(tried_models)
        own_used = _model_used_in(res)
        if own_used and own_used not in own_tried:
            own_tried.append(own_used)

        if len(candidates) > 1 and (candidates[0][1] - candidates[1][1]) < 0.1:
            # Ambiguous — ask user to choose
            buttons = []
            for p, ratio in candidates[:5]:
                buttons.append([
                    InlineKeyboardButton(
                        f"@{p['username']} ({p.get('game_nickname', '—')}) {int(ratio*100)}%",
                        callback_data=f"ocr_pick:{p['id']}:{my_score}:{opp_score}:{t_type}{tid_tag}",
                    )
                ])
            ambig_retry = _retry_button_row(
                ctx,
                file_id=file_id,
                tried_models=own_tried,
                target_tournament=target_tournament,
                reporter_id=reporter["id"] if reporter else None,
                caption=caption,
                panel_kind="ambig",
            )
            if ambig_retry:
                buttons.append(ambig_retry)
            buttons.append([
                InlineKeyboardButton("❌ Отмена", callback_data="ocr_cancel")
            ])
            await msg.reply_text(
                debug + "\n\nКто из них соперник?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        opponent = candidates[0][0]
        ratio = candidates[0][1]

        # ── Auto-submit when confidence ≥ 60% (single-photo mode) ───────
        # Skip the "✅ Всё верно" confirmation button — send directly to
        # admin review (or auto-confirm depending on tournament settings).
        # Same behaviour as album mode: one photo = one result, no extra tap.
        if ratio >= 0.60:
            ctx.user_data[f"ocr_extra_{file_id}"] = {
                "league_plate": res.league_plate,
                "raw": res.raw_texts,
                "screenshot_hash": screenshot_hash,
                "goals": list(getattr(res, "goals", []) or []),
                "mgi": mgi,
                "team1": res.team1,
                "team2": res.team2,
                "score1": res.score1,
                "score2": res.score2,
            }
            match_id_auto = await _do_report(
                update, ctx,
                reporter=reporter,
                opponent=opponent,
                s1=int(my_score),
                s2=int(opp_score),
                tournament_type=t_type,
                tournament=target_tournament,
                screenshot_hash=screenshot_hash,
                screenshot_file_id=file_id,
                ocr_goals=list(getattr(res, "goals", []) or []),
                suppress_result_message=False,
            )
            if match_id_auto and mgi:
                st = _album_state(ctx, mgi)
                if st is not None:
                    st["match_id"] = match_id_auto
                    st["p1_id"] = reporter["id"]
                    st["p2_id"] = opponent["id"]
            return

        # Low confidence (< 60%) — show confirmation panel with retry
        own_retry = _retry_button_row(
            ctx,
            file_id=file_id,
            tried_models=own_tried,
            target_tournament=target_tournament,
            reporter_id=reporter["id"] if reporter else None,
            caption=caption,
            panel_kind="own",
        )
        own_rows = [
            [
                InlineKeyboardButton(
                    "✅ Всё верно — отправить",
                    callback_data=(
                        f"ocr_pick:{opponent['id']}:{my_score}:{opp_score}:{t_type}"
                        f":{target_tournament['id'] if target_tournament else '0'}"
                        f":{_retry_token_for(file_id)}"
                    ),
                ),
                InlineKeyboardButton("❌ Отмена", callback_data="ocr_cancel"),
            ]
        ]
        if own_retry:
            own_rows.append(own_retry)
        kb = InlineKeyboardMarkup(own_rows)
        await msg.reply_text(
            debug + (
                f"\n\n👤 Соперник: {mention(opponent['username'])} "
                f"(<i>{opponent.get('game_nickname','—')}</i>, совпадение {int(ratio*100)}%)\n\n"
                f"⚠️ Низкая уверенность — подтверди отправку результата."
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )

        # Stash extra stats for later (in case the user confirms)
        ctx.user_data[f"ocr_extra_{file_id}"] = {
            "league_plate": res.league_plate,
            "raw": res.raw_texts,
            "screenshot_hash": screenshot_hash,
            "goals": list(getattr(res, "goals", []) or []),
            "mgi": mgi,
            "team1": res.team1,
            "team2": res.team2,
            "score1": res.score1,
            "score2": res.score2,
        }

        # Record this photo as the album's "primary" so a subsequent
        # photo with matching teams + score gets recognised as a
        # continuation. We store the OCR-extracted goals so even if the
        # user hasn't yet confirmed, we still merge incoming photos
        # against them and the final ``_do_report`` writes the merged
        # set into the match.
        if album_state is not None:
            album_state.setdefault("sig", (
                res.team1, res.team2, res.score1, res.score2,
            ))
            album_state.setdefault(
                "goals", list(getattr(res, "goals", []) or []),
            )
            album_state.setdefault("primary_file_id", file_id)

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 🔄 «Другой моделью» — re-runs OCR on the same screenshot using the
# next un-tried model from ``AI_FALLBACK_MODELS``. Triggered from any
# of the confirmation panels (own match / admin record / ambiguous
# opponent picker).
# ─────────────────────────────────────────────────────────────────────────────


async def cb_retry_ocr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    token = data.split(":", 1)[1] if ":" in data else ""
    state = ctx.user_data.get(f"ocr_retry_{token}") if token else None
    if not state:
        try:
            await query.edit_message_text(
                "⚠️ Сессия retry-OCR истекла. Отправь скрин заново.",
            )
        except TelegramError:
            pass
        return

    file_id = state.get("file_id")
    tried = list(state.get("tried") or [])
    caption = state.get("caption") or ""
    target_tid = state.get("target_tournament_id")
    reporter_id = state.get("reporter_id")
    if not file_id:
        try:
            await query.edit_message_text("⚠️ Внутренняя ошибка retry: нет file_id.")
        except TelegramError:
            pass
        return

    # Pick the next un-tried model.
    next_model = next(
        (m for m in AI_FALLBACK_MODELS if m not in tried), None
    )
    if not next_model:
        try:
            await query.edit_message_text(
                "🛑 Все модели уже перепробованы. "
                "Попробуй прислать скрин заново или внеси вручную через /report.",
            )
        except TelegramError:
            pass
        ctx.user_data.pop(f"ocr_retry_{token}", None)
        return

    # Re-resolve reporter & tournament from stashed IDs (live records,
    # in case the user's nick or the tournament name changed since).
    reporter = (
        db.get_player_by_id(reporter_id) if reporter_id is not None else None
    )
    if reporter is None:
        # Fall back to the calling user — they obviously have access to
        # the panel since they clicked the button.
        reporter = _player_from_user(update.effective_user)
    target_tournament = (
        db.get_tournament(target_tid) if target_tid else None
    )

    # Edit current panel to a "🔄 Перепознаю…" status. Strip the
    # keyboard so the user can't double-click ✅ on stale data.
    try:
        await query.edit_message_text(
            f"🔄 Перепознаю скрин моделью <code>{html.escape(next_model)}</code>…",
            parse_mode="HTML",
        )
    except TelegramError:
        # Edit may fail if the original panel was a forwarded message,
        # was deleted, or hit the 48h edit window. Nothing fatal — the
        # new panel will still appear as a fresh message.
        pass

    new_tried = tried + [next_model]
    try:
        await _process_match_photo(
            update, ctx,
            reporter=reporter,
            target_tournament=target_tournament,
            file_id=file_id,
            caption=caption,
            ai_models_override=[next_model],
            tried_models=new_tried,
        )
    except Exception as e:
        log.exception("retry OCR failed: %s", e)
        await send(
            update,
            f"❌ Не получилось перепознать ({html.escape(next_model)}): "
            f"<code>{html.escape(str(e)[:200])}</code>",
        )


# ─────────────────────────────────────────────────────────────────────────────
# /confirm
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        await send(update, "⚠️ Нет матчей, ожидающих твоего подтверждения.")
        return

    m = dict(m)
    await _after_opponent_confirm(update, ctx, m)


# ─────────────────────────────────────────────────────────────────────────────
# Callback handler (inline buttons)
# ─────────────────────────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError as e:
        log.warning("query.answer() failed (probably stale): %s", e)
    data = query.data

    if data == "ocr_cancel":
        await query.edit_message_text("Отменено.")
        ctx.user_data.pop("pending_report", None)
        ctx.user_data.pop("pending_photo", None)
        return

    # ── Champions / Hall of Fame inline browser ──────────────────────────
    if data.startswith("chmp:"):
        from handlers.champions import handle_callback as cb_champions
        await cb_champions(update, ctx)
        return

    # ── Tournament template wizard callbacks ─────────────────────────────
    if data.startswith("tpl_cat:"):
        from handlers.templates import cb_template_category
        await cb_template_category(update, ctx)
        return
    if data.startswith("tpl_pick:"):
        from handlers.templates import cb_template_pick
        await cb_template_pick(update, ctx)
        return
    if data.startswith("tpl_custom:"):
        from handlers.templates import cb_template_custom_pick
        await cb_template_custom_pick(update, ctx)
        return
    if data.startswith("tpl_draw:"):
        from handlers.templates import cb_template_draw_mode
        await cb_template_draw_mode(update, ctx)
        return
    if data.startswith("tpl_create:"):
        from handlers.templates import cb_template_create
        await cb_template_create(update, ctx)
        return
    if data.startswith("tpl_cust_create:"):
        from handlers.templates import cb_template_custom_create
        await cb_template_custom_create(update, ctx)
        return
    if data.startswith("tpl_cust_base:"):
        from handlers.templates import cb_custom_base
        await cb_custom_base(update, ctx)
        return
    if data.startswith("tpl_cfg:"):
        from handlers.templates import cb_config_param
        await cb_config_param(update, ctx)
        return
    if data.startswith("tpl_cust_final:"):
        from handlers.templates import cb_custom_final_create
        await cb_custom_final_create(update, ctx)
        return
    if data == "tpl_cust_save":
        from handlers.templates import cb_custom_save
        await cb_custom_save(update, ctx)
        return
    if data == "tpl_back_main":
        from handlers.templates import cb_back_main
        await cb_back_main(update, ctx)
        return
    # ── Manual draw callbacks ────────────────────────────────────────────
    if data.startswith("mdraw_first:"):
        from handlers.templates import cb_manual_draw_first
        await cb_manual_draw_first(update, ctx)
        return
    if data.startswith("mdraw_pair:"):
        from handlers.templates import cb_manual_draw_pair
        await cb_manual_draw_pair(update, ctx)
        return
    if data == "mdraw_auto_rest":
        from handlers.templates import cb_manual_draw_auto_rest
        await cb_manual_draw_auto_rest(update, ctx)
        return
    if data == "mdraw_cancel":
        from handlers.templates import cb_manual_draw_cancel
        await cb_manual_draw_cancel(update, ctx)
        return

    if data.startswith("retryocr:"):
        await cb_retry_ocr(update, ctx)
        return

    if data in ("my_deadlines", "my_matches"):
        # Profile shortcut buttons. Synthesize the equivalent slash
        # invocation so we reuse the formatting logic.
        ctx.args = []
        if data == "my_deadlines":
            await cmd_my_deadlines(update, ctx)
        else:
            await cmd_matches(update, ctx)
        return

    if data.startswith("tbl_pick:") or data in (
        "tbl_cancel", "tbl_noop", "tbl_show_finished", "tbl_hide_finished",
    ):
        await cb_table_pick(update, ctx)
        return
    if data.startswith("tbl_view:"):
        await cb_table_view(update, ctx)
        return
    if data.startswith("po_pick:") or data in (
        "po_cancel", "po_noop", "po_show_finished", "po_hide_finished",
    ):
        await cb_playoff_pick(update, ctx)
        return

    if data in ("kb:hide", "kb:show"):
        # Inline-toggle for the bottom reply keyboard. Avoids needing the
        # Telegram-native toggle icon, which is only shown when a
        # ReplyKeyboardMarkup is currently active.
        user = update.effective_user
        chat = update.effective_chat
        p = _player_from_user(user) if user else None
        hide = (data == "kb:hide")
        if p:
            try:
                db.set_no_keyboard_preference(p["id"], hide)
            except Exception:
                log.exception("kb toggle: set_no_keyboard_preference failed")
        try:
            if hide:
                await query.edit_message_text(
                    "🫥 Нижняя панель скрыта.\n"
                    "Снова показать — /keyboard или /show_keyboard.",
                )
            else:
                await query.edit_message_text(
                    "✅ Нижняя панель снова видна.\n"
                    "Скрыть обратно — /keyboard или /hide_keyboard.",
                )
        except TelegramError:
            pass
        # Now actually push a fresh ReplyKeyboardMarkup (or a remove) so
        # Telegram updates the on-screen state immediately.
        if chat is None:
            return
        try:
            if hide:
                await ctx.bot.send_message(
                    chat.id, "🫥",
                    reply_markup=ReplyKeyboardRemove(),
                )
            else:
                await ctx.bot.send_message(
                    chat.id, "📋 Готово.",
                    reply_markup=main_menu_kb(user.id if user else None),
                )
        except Exception:
            log.exception("kb toggle: send_message failed")
        return

    if data.startswith("pickrep:"):
        # User picked a tournament for a /report invocation that didn't
        # specify one. Resume the report with that tournament forced.
        try:
            tid = int(data.split(":", 1)[1])
        except ValueError:
            return
        sess = ctx.user_data.pop("pending_report", None)
        if not sess:
            await query.edit_message_text("❌ Сессия истекла, повтори /report.")
            return
        t = get_tournament(tid)
        if not t:
            await query.edit_message_text("❌ Турнир не найден.")
            return
        reporter = _player_from_user(update.effective_user)
        opponent = get_player_by_id(int(sess["opp_id"]))
        if not reporter or not opponent:
            await query.edit_message_text("❌ Не нашёл игроков, повтори /report.")
            return
        # Don't pre-edit the picker with "📨 Отправляю …" — we'll edit it
        # exactly once with the final result. ``suppress_result_message``
        # keeps ``_do_report`` from posting its own confirmation, so the
        # whole /report flow ends up at exactly one bot message.
        await _do_report(
            update, ctx,
            reporter=reporter, opponent=opponent,
            s1=int(sess["s1"]), s2=int(sess["s2"]),
            tournament_type=t["tournament_type"],
            tournament=t,
            suppress_result_message=True,
        )
        result_text = ctx.user_data.pop(
            "_last_report_result_text",
            f"📨 Отправлено в <b>{html.escape(t['name'])}</b> "
            f"(ID {t['id']}, {t_full_label(t)}).",
        )
        try:
            await query.edit_message_text(result_text, parse_mode="HTML")
        except TelegramError:
            await ctx.bot.send_message(
                update.effective_chat.id, result_text, parse_mode="HTML",
            )
        return

    if data == "wo_cancel":
        try:
            await query.edit_message_text("❌ Отменено. ТП не засчитан.")
        except TelegramError:
            pass
        return

    if data.startswith("woall:"):
        # woall:<loser_id>:<tournament_id> — bulk walkover.
        if not is_admin(update.effective_user.id):
            await query.edit_message_text("❌ Только админ.")
            return
        parts = data.split(":")
        try:
            loser_id = int(parts[1]); tid = int(parts[2])
        except (IndexError, ValueError):
            return
        pendings = _list_pending_matches_for(loser_id, tid)
        if not pendings:
            await query.edit_message_text("✅ Нечего засчитывать — pending-матчей не осталось.")
            return
        applied = 0
        failed = 0
        for m in pendings:
            try:
                apply_walkover(m["id"], loser_id)
                applied += 1
            except Exception as e:
                log.warning("bulk walkover failed for match %s: %s", m["id"], e)
                failed += 1
        loser_p = get_player_by_id(loser_id)
        loser_lbl = mention(loser_p["username"]) if loser_p else str(loser_id)
        t = get_tournament(tid)
        t_lbl = html.escape(t["name"]) if t else f"ID {tid}"
        msg = (
            f"⚠️ Bulk-ТП применён.\n\n"
            f"Игрок: {loser_lbl}\n"
            f"Турнир: <b>{t_lbl}</b>\n"
            f"Засчитано: <b>{applied}</b> матч(ей) (0:3)"
        )
        if failed:
            msg += f"\n⚠️ Ошибок: {failed}"
        try:
            await query.edit_message_text(msg, parse_mode="HTML")
        except TelegramError:
            await send(update, msg)
        # Auto-advance once at the end and broadcast the new stage.
        try:
            advanced = _maybe_auto_advance(ctx, tid)
        except Exception as e:
            log.warning("auto-advance after bulk walkover failed: %s", e)
            advanced = False
        if advanced:
            await _announce_stage_advance(
                ctx, tid, _current_playoff_stage(tid)
            )
        return

    if data.startswith("tnall:"):
        # tnall:<tournament_id> — bulk technical nil (0:0).
        if not is_admin(update.effective_user.id):
            await query.edit_message_text("❌ Только админ.")
            return
        parts = data.split(":")
        try:
            tid = int(parts[1])
        except (IndexError, ValueError):
            return
        all_ms = get_tournament_matches(tid)
        pendings = [
            m for m in all_ms
            if m.get("status") == "pending"
            and m.get("player1_id") != m.get("player2_id")
        ]
        if not pendings:
            await query.edit_message_text(
                "✅ Нечего засчитывать — pending-матчей не осталось."
            )
            return
        applied = 0
        failed = 0
        for m in pendings:
            try:
                update_match(
                    m["id"],
                    score1=0, score2=0,
                    status="confirmed",
                    reported_by=update.effective_user.id,
                )
                apply_result(m["id"])
                applied += 1
            except Exception as e:
                log.warning("bulk tech-nil failed for match %s: %s", m["id"], e)
                failed += 1
        t = get_tournament(tid)
        t_lbl = html.escape(t["name"]) if t else f"ID {tid}"
        msg = (
            f"⚠️ Технический ноль применён.\n\n"
            f"Турнир: <b>{t_lbl}</b>\n"
            f"Засчитано: <b>{applied}</b> матч(ей) (0:0)"
        )
        if failed:
            msg += f"\n⚠️ Ошибок: {failed}"
        try:
            await query.edit_message_text(msg, parse_mode="HTML")
        except TelegramError:
            await send(update, msg)
        try:
            log_tournament_action(
                tid,
                actor_telegram_id=update.effective_user.id,
                actor_username=update.effective_user.username,
                action="tech_nil_bulk",
                details=f"applied={applied} failed={failed} score=0:0",
            )
        except Exception:
            pass
        try:
            advanced = _maybe_auto_advance(ctx, tid)
        except Exception as e:
            log.warning("auto-advance after bulk tech-nil failed: %s", e)
            advanced = False
        if advanced:
            await _announce_stage_advance(
                ctx, tid, _current_playoff_stage(tid)
            )
        return

    if data.startswith("audit_undo:"):
        from handlers.match import cb_audit_undo
        await cb_audit_undo(update, ctx)
        return

    if data.startswith("audit_pg:"):
        from handlers.match import cb_audit_page
        await cb_audit_page(update, ctx)
        return

    if data.startswith("asel:"):
        from handlers.match import cb_audit_select_tournament
        await cb_audit_select_tournament(update, ctx)
        return

    if data.startswith("aflt:"):
        from handlers.match import cb_audit_filter_type
        await cb_audit_filter_type(update, ctx)
        return

    if data.startswith("aadm:"):
        from handlers.match import cb_audit_filter_admin
        await cb_audit_filter_admin(update, ctx)
        return

    if data.startswith("wo:"):
        # wo:<match_id>:<loser_id>
        if not is_admin(update.effective_user.id):
            await query.edit_message_text("❌ Только админ.")
            return
        parts = data.split(":")
        try:
            mid = int(parts[1]); loser_id = int(parts[2])
        except (IndexError, ValueError):
            return
        await _do_walkover(update, ctx, mid, loser_id, send_via=query)
        return

    if data.startswith("tcg:"):
        # Tournament-creation group-pickers (groups_count and target_group_size).
        # tcg:groups:<tid>:<n>   — set groups_count (n=0 means leave auto)
        # tcg:size:<tid>:<n>     — set target_group_size (n=0 means leave auto)
        # tcg:cancel:<tid>       — close the picker silently
        parts = data.split(":")
        if len(parts) < 3:
            return
        action = parts[1]
        try:
            tid = int(parts[2])
        except ValueError:
            return
        t = get_tournament(tid)
        if not t:
            await query.edit_message_text("❌ Турнир не найден.")
            return
        if not _can_manage_tournament(update.effective_user.id, t):
            await query.edit_message_text("❌ Только создатель турнира или админ.")
            return

        if action == "cancel":
            await query.edit_message_text(
                "❌ Отменено. Жеребьёвка останется на авто-настройках "
                "(их можно поменять до /start_tournament).",
            )
            return

        if action == "groups":
            try:
                n = int(parts[3])
            except (IndexError, ValueError):
                return
            if n > 0:
                update_tournament(tid, groups_count=max(1, min(32, n)))
            # Now ask for players-per-group. The picker offers a
            # selection of round numbers up to 100 (the new cap, 2026-05)
            # — admins can also type a specific value via the CLI flag
            # ``/create_tournament <name> <type> <groups> <per_group>``.
            sizes = [2, 3, 4, 5, 6, 8, 12, 20, 50, 100]
            rows = [[
                InlineKeyboardButton(str(s), callback_data=f"tcg:size:{tid}:{s}")
                for s in sizes[:6]
            ], [
                InlineKeyboardButton(str(s), callback_data=f"tcg:size:{tid}:{s}")
                for s in sizes[6:]
            ]]
            rows.append([
                InlineKeyboardButton("⏭ Авто", callback_data=f"tcg:size:{tid}:0"),
            ])
            rows.append([
                InlineKeyboardButton("❌ Отмена", callback_data=f"tcg:cancel:{tid}"),
            ])
            chosen = "Авто" if n == 0 else str(n)
            await query.edit_message_text(
                f"🏟 Групп: <b>{chosen}</b>.\n\n"
                f"👥 Сколько игроков должно быть в каждой группе?\n"
                f"<i>Если не выберешь — жеребьёвка раскидает поровну.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if action == "size":
            try:
                n = int(parts[3])
            except (IndexError, ValueError):
                return
            if n > 0:
                update_tournament(tid, target_group_size=max(2, min(100, n)))
            t = get_tournament(tid)
            gc = int(t.get("groups_count") or 0)
            tgs = int(t.get("target_group_size") or 0)
            summary_parts = []
            if gc > 0:
                summary_parts.append(f"<b>{gc}</b> групп")
            if tgs > 0:
                summary_parts.append(f"по <b>{tgs}</b> игроков")
            summary = ", ".join(summary_parts) if summary_parts else "авто-распределение"
            await query.edit_message_text(
                f"✅ Жеребьёвка настроена: {summary}.\n\n"
                f"Теперь добавь игроков (<code>/add_player @user1, @user2, ...</code>) "
                f"и запусти жеребьёвку: <code>/start_tournament</code>.",
                parse_mode="HTML",
            )
            return
        return

    if data.startswith("apxconfirm:"):
        # Admin proxy-report from photo: register a match between two
        # OTHER players (the admin isn't a participant). Mirrors the
        # /admin_report flow but with auto-extracted score+nicknames.
        if not is_admin(update.effective_user.id):
            await query.edit_message_text("❌ Только админ.")
            return
        # apxconfirm:<p1_id>:<p2_id>:<s1>:<s2>[:<tid>]
        parts = data.split(":")
        if len(parts) < 5:
            await query.edit_message_text("❌ Битый callback.")
            return
        try:
            p1_id = int(parts[1]); p2_id = int(parts[2])
            s1 = int(parts[3]);    s2 = int(parts[4])
        except ValueError:
            await query.edit_message_text("❌ Битый callback.")
            return
        tid_apx = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None
        p1 = get_player_by_id(p1_id)
        p2 = get_player_by_id(p2_id)
        if not p1 or not p2:
            await query.edit_message_text("❌ Игроки не найдены в БД.")
            return
        # Look for an existing pending/reported match first. In playoff
        # albums prefer the lowest-leg ``pending`` row between the pair —
        # otherwise concurrent admin confirmations would all stomp on the
        # same already-claimed leg.
        apx_t = get_tournament(tid_apx) if tid_apx else None
        apx_t_stage = (apx_t.get("stage") or "groups") if apx_t else "groups"
        existing = None
        if tid_apx and apx_t_stage == "playoff":
            existing = _find_pending_playoff_leg(p1["id"], p2["id"], tid_apx)
        if existing is None:
            existing = get_pending_match(p1["id"], p2["id"], tid_apx)
        if existing:
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
            # No empty leg — for a playoff tournament, spawn the next leg
            # in the correct stage (e.g. ``sf``, leg 3) rather than the
            # bogus ``stage='playoff'`` orphan the old code produced.
            apx_match_stage = "group" if apx_t_stage == "groups" else apx_t_stage
            apx_leg = 1
            if tid_apx and apx_t_stage == "playoff":
                spec = _next_playoff_leg_spec(p1["id"], p2["id"], tid_apx)
                if spec is not None:
                    apx_match_stage, apx_leg = spec
            match_id = db.create_match(
                tid_apx or 0, p1["id"], p2["id"],
                stage=apx_match_stage, round_num=1, leg=apx_leg,
            )
            update_match(
                match_id,
                score1=s1, score2=s2,
                status="confirmed",
                reported_by=update.effective_user.id,
            )
        # Pull extra stats / screenshot hash / goals if available.
        for k, v in list(ctx.user_data.items()):
            if k.startswith("ocr_extra_") and isinstance(v, dict):
                if v.get("screenshot_hash"):
                    update_match(match_id, screenshot_hash=v["screenshot_hash"])
                    try:
                        db.record_processed_screenshot(
                            v["screenshot_hash"],
                            tid_apx,
                            str(update.effective_chat.id) if update.effective_chat else None,
                            match_id,
                            update.effective_user.id,
                        )
                    except Exception:
                        log.exception("record_processed_screenshot failed (apx)")
                # Persist OCR goal events for top-scorer leaderboards.
                try:
                    _persist_ocr_goals(match_id, p1, p2, v.get("goals"))
                except Exception:
                    log.exception("persist ocr goals failed (apx)")
                ctx.user_data.pop(k, None)
                break
        try:
            summary = apply_result(match_id)
        except Exception:
            log.exception("apply_result failed in apxconfirm")
            summary = {}
        d1 = summary.get("delta1", 0); d2 = summary.get("delta2", 0)
        elo1_after = summary.get("elo1_after", "?")
        elo2_after = summary.get("elo2_after", "?")
        scope = ""
        announce_stage_apx: str | None = summary.get("advanced_stage")
        if tid_apx:
            t_apx = get_tournament(tid_apx)
            if t_apx:
                scope = f" в турнире <b>{html.escape(t_apx['name'])}</b> (ID {t_apx['id']})"
                # Try auto-advance just like cmd_admin_report does.
                try:
                    if _maybe_auto_advance(ctx, tid_apx) and not announce_stage_apx:
                        announce_stage_apx = _current_playoff_stage(tid_apx)
                except Exception:
                    log.warning("auto-advance after apxconfirm failed", exc_info=True)
        await query.edit_message_text(
            f"✅ Записано (админ-режим): {mention(p1['username'])} "
            f"<b>{s1}:{s2}</b> {mention(p2['username'])}{scope}.\n"
            f"📈 ELO: {mention(p1['username'])}: <b>{elo1_after}</b> "
            f"({arrow(d1)}) · {mention(p2['username'])}: <b>{elo2_after}</b> "
            f"({arrow(d2)})",
            parse_mode="HTML",
        )
        if tid_apx and announce_stage_apx:
            await _announce_stage_advance(ctx, tid_apx, announce_stage_apx)
        return

    if data.startswith("pickphoto:"):
        # User picked a tournament for an unbound DM photo upload before OCR
        # ran. We still have the original photo's file_id in user_data, so
        # download + process it inline using the chosen tournament.
        try:
            tid = int(data.split(":", 1)[1])
        except ValueError:
            return
        t = get_tournament(tid)
        if not t:
            await query.edit_message_text("❌ Турнир не найден.")
            return
        sess = ctx.user_data.pop("pending_photo", None)
        if not sess or not sess.get("file_id"):
            await query.edit_message_text("❌ Сессия истекла, пришли скрин ещё раз.")
            return
        reporter = _player_from_user(update.effective_user)
        if not reporter:
            await query.edit_message_text("❌ Сначала зарегистрируйся: /register")
            return
        if not reporter.get("game_nickname"):
            await query.edit_message_text(
                "❌ Чтобы я мог понять, что это твой матч, укажи свой игровой ник: "
                "<code>/setnick TwoiNick</code>",
                parse_mode="HTML",
            )
            return
        try:
            await query.edit_message_text(
                f"✅ Турнир выбран: <b>{html.escape(t['name'])}</b> "
                f"(ID {t['id']}). Распознаю скрин…",
                parse_mode="HTML",
            )
        except TelegramError:
            pass
        await _process_match_photo(
            update, ctx,
            reporter=reporter,
            target_tournament=t,
            file_id=sess["file_id"],
            caption="",
        )
        return

    if (
        data == "fin_cancel"
        or data.startswith("fin_t:")
        or data.startswith("fin_t_skip3:")
    ):
        # The /finish_tournament callback already calls query.answer() itself,
        # but doing it again is harmless and was already done at the top.
        await cb_finish_tournament(update, ctx)
        return

    if data == "sim_cancel" or data.startswith("sim_t:"):
        await cb_simulate(update, ctx)
        return

    if data.startswith("fin_ask:"):
        # "Finish" button from /tournaments — show a confirmation prompt.
        try:
            tid = int(data.split(":", 1)[1])
        except ValueError:
            return
        t = get_tournament(tid)
        if not t:
            await query.message.reply_text("❌ Турнир не найден.")
            return
        if not _can_manage_tournament(update.effective_user.id, t):
            await query.message.reply_text("❌ Только создатель или админ.")
            return
        if t.get("stage") == "finished":
            await query.message.reply_text(
                f"ℹ️ Турнир <b>{t['name']}</b> уже завершён.", parse_mode="HTML"
            )
            return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, завершить", callback_data=f"fin_t:{t['id']}"),
            InlineKeyboardButton("❌ Отмена",        callback_data="fin_cancel"),
        ]])
        await query.message.reply_text(
            f"⚠️ Завершить турнир <b>{t['name']}</b> (ID {t['id']}, "
            f"{t_full_label(t)})?\n"
            f"После завершения новые матчи в нём приниматься не будут.",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    if data == "feedback_start":
        ctx.user_data["awaiting_feedback"] = True
        await query.message.reply_text(
            "🐞💡 Напиши свой баг или предложение одним сообщением (можно с фото).\n"
            "Чтобы отменить — /cancel.",
        )
        return

    if data.startswith("fb_reply:"):
        target_tg_id = int(data.split(":", 1)[1])
        ctx.user_data["awaiting_fb_reply_to"] = target_tg_id
        await query.answer()
        await query.message.reply_text(
            f"💬 Напиши ответ пользователю (tg_id={target_tg_id}).\n"
            "Чтобы отменить — /cancel.",
        )
        return

    if data == "request_admin":
        user = update.effective_user
        if is_admin(user.id):
            await query.answer("Ты уже админ.", show_alert=True)
            return
        if not ADMIN_IDS:
            await query.answer("Нет root-админов — некому отправить запрос.", show_alert=True)
            return
        user_tag = f"@{user.username}" if user.username else f"id {user.id}"
        name = user.full_name or "—"
        text = (
            f"👮 <b>Запрос на админку</b>\n"
            f"От: {user_tag} (<i>{html.escape(name)}</i>, tg_id={user.id})\n"
            f"{'─'*30}\n"
            f"Пользователь запрашивает права администратора.\n\n"
            f"Чтобы выдать: <code>/grant_admin {user.id}</code>"
        )
        delivered = 0
        for admin_id in ADMIN_IDS:
            try:
                await ctx.bot.send_message(admin_id, text, parse_mode="HTML")
                delivered += 1
            except Exception:
                pass
        if delivered:
            await query.answer(
                f"✅ Запрос отправлен {delivered}/{len(ADMIN_IDS)} админ(ам).",
                show_alert=True,
            )
        else:
            await query.answer("⚠️ Не удалось доставить запрос.", show_alert=True)
        return

    # ── Menu navigation ───────────────────────────────────────────────────
    if data == "menu:home":
        try:
            await query.edit_message_text(
                WELCOME_TEXT,
                parse_mode="HTML",
                reply_markup=main_menu_inline_kb(update.effective_user.id),
            )
        except TelegramError:
            await query.message.reply_text(
                WELCOME_TEXT,
                parse_mode="HTML",
                reply_markup=_menu_markup_for(update, update.effective_user.id),
            )
        return

    # ── Group inline-menu (mirrors the main menu in groups) ───────────────
    if data.startswith("gmenu:"):
        section = data.split(":", 1)[1]
        handler = MENU_DISPATCH_INLINE.get(_GMENU_TO_LABEL.get(section))
        if handler:
            _wizard_clear(ctx)
            ctx.user_data.pop("awaiting_feedback", None)
            ctx.user_data.pop("awaiting_fb_reply_to", None)
            await handler(update, ctx)
        return

    # ── Tournaments submenu ───────────────────────────────────────────────
    if data == "t:list":
        actives = get_active_tournaments()
        if not actives:
            await query.message.reply_text("Нет активных турниров.")
            return
        # Resolve the calling user's player row once — the join/leave
        # buttons depend on whether they're already in the tournament.
        viewer_player = _player_from_user(update.effective_user)
        viewer_pid = viewer_player["id"] if viewer_player else None
        for t in actives:
            members = get_tournament_players(t["id"])
            block = [
                f"🏆 <b>{t['name']}</b> [{t_full_label(t)}]",
                f"Игроков: <b>{len(members)}</b>, этап: <i>{t['stage']}</i>",
            ]
            if t.get("description"):
                block.append(f"📝 {t['description']}")
            if t.get("required_channel"):
                block.append(f"🔗 Канал: <b>{t['required_channel']}</b>")
            kb_rows = []

            # Self-registration row — shown to ANY user who has a
            # ``players`` row (i.e. used /register at least once). Only
            # while the tournament's signup window is open and matches
            # haven't been generated yet, otherwise late joins would
            # need to slot into already-running groups (use
            # ``/add_player`` for that — admins only).
            stage_lower = (t.get("stage") or "groups").lower()
            signups_open = (
                int(t.get("open_signup") or 0)
                and stage_lower in ("groups", "")
                and len(get_tournament_matches(t["id"])) == 0
            )
            if viewer_pid is not None and signups_open:
                already_in = is_player_in_tournament(t["id"], viewer_pid)
                if already_in:
                    kb_rows.append([
                        InlineKeyboardButton(
                            "🚪 Покинуть турнир",
                            callback_data=f"t:leave:{t['id']}",
                        ),
                    ])
                else:
                    kb_rows.append([
                        InlineKeyboardButton(
                            "🙋 Записаться",
                            callback_data=f"t:join:{t['id']}",
                        ),
                    ])

            if _can_manage_tournament(update.effective_user.id, t):
                kb_rows.append([
                    InlineKeyboardButton("➕ Добавить игрока", callback_data=f"t:addplayer:{t['id']}"),
                    InlineKeyboardButton("🏷 Команды", callback_data=f"team:list:{t['id']}"),
                ])
                kb_rows.append([
                    InlineKeyboardButton("📝 Описание",   callback_data=f"t:setdesc:{t['id']}"),
                    InlineKeyboardButton("🔗 Канал",      callback_data=f"t:setchan:{t['id']}"),
                ])
                kb_rows.append([
                    InlineKeyboardButton("▶️ Старт",       callback_data=f"t:start:{t['id']}"),
                    InlineKeyboardButton("🏁 Плей-офф",   callback_data=f"t:playoff:{t['id']}"),
                ])
            kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None
            await query.message.reply_text("\n".join(block), parse_mode="HTML", reply_markup=kb)
        return

    # ── Finished-tournaments browser (🏁 Итоги турниров) ──────────────────
    if data.startswith("t:finished"):
        await cb_finished_tournaments(update, ctx)
        return
    if data == "t:compare":
        await cb_compare_tournaments(update, ctx)
        return
    if data.startswith("t:facts:"):
        await cb_reroll_facts(update, ctx)
        return
    if data == "db:export" or data == "db:export_bot" or data == "db:import" or data == "db:cancel_import" or data.startswith("db:confirm_import:"):
        await cb_db_buttons(update, ctx)
        return
    if (data.startswith("t:summary:")
            or data.startswith("t:summaryai:")
            or data.startswith("t:summarytg:")):
        await cb_tournament_summary_button(update, ctx)
        return

    # Self-signup: a registered player joins/leaves an open tournament.
    # These callbacks intentionally bypass /add_player (admin-only) — but
    # the signup window is gated by ``open_signup`` AND
    # "no matches generated yet", so it's safe to let any user touch the
    # tournament_players table here.
    if data.startswith("t:join:") or data.startswith("t:leave:"):
        try:
            tid = int(data.split(":", 2)[2])
        except (ValueError, IndexError):
            await query.message.reply_text("❌ Некорректный ID турнира.")
            return
        t = get_tournament(tid)
        if not t:
            await query.message.reply_text("❌ Турнир не найден.")
            return
        player = _player_from_user(update.effective_user)
        if not player:
            await query.message.reply_text(
                "❌ Сначала зарегистрируйся: /register"
            )
            return
        stage_lower = (t.get("stage") or "groups").lower()
        signups_open = (
            int(t.get("open_signup") or 0)
            and stage_lower in ("groups", "")
            and len(get_tournament_matches(tid)) == 0
        )
        if not signups_open:
            await query.message.reply_text(
                "❌ Запись на этот турнир уже закрыта."
            )
            return
        if data.startswith("t:join:"):
            if is_player_in_tournament(tid, player["id"]):
                await query.message.reply_text(
                    f"ℹ️ Ты уже записан в турнир <b>{t['name']}</b>.",
                    parse_mode="HTML",
                )
                return
            # "?" is the lobby group — /start_tournament's draw
            # re-assigns everyone in '?' to real groups (A, B, …).
            add_player_to_tournament(tid, player["id"], "?")
            members = get_tournament_players(tid)
            await query.message.reply_text(
                f"✅ Ты записан в турнир <b>{t['name']}</b>!\n"
                f"Игроков уже: <b>{len(members)}</b>. "
                f"Дождись жеребьёвки от админа.",
                parse_mode="HTML",
            )
            return
        # t:leave
        if not is_player_in_tournament(tid, player["id"]):
            await query.message.reply_text(
                f"ℹ️ Ты не записан в турнир <b>{t['name']}</b>.",
                parse_mode="HTML",
            )
            return
        remove_player_from_tournament(tid, player["id"])
        await query.message.reply_text(
            f"🚪 Ты покинул турнир <b>{t['name']}</b>.",
            parse_mode="HTML",
        )
        return

    if data == "t:table":
        ctx.args = []
        await cmd_table(update, ctx)
        return

    if data == "t:bracket":
        ctx.args = []
        await cmd_playoff(update, ctx)
        return

    if data == "t:bomb":
        ctx.args = []
        await cmd_table_bomb(update, ctx)
        return

    if data == "t:tabletxt":
        ctx.args = ["text"]
        await cmd_table(update, ctx)
        return

    if data == "t:brackettxt":
        ctx.args = ["text"]
        await cmd_playoff(update, ctx)
        return

    if data == "t:create":
        if not is_admin(update.effective_user.id):
            await query.message.reply_text(
                "❌ Создавать турниры могут только админы."
            )
            return
        _wizard_set(ctx, "create_t_name")
        await query.message.reply_text("✏️ Напиши название турнира:")
        return

    if data.startswith("t:create_finish:"):
        t_type = data.split(":", 2)[2]
        wiz = _wizard_get(ctx)
        name = (wiz or {}).get("data", {}).get("name") if wiz else None
        if not name:
            await query.edit_message_text("❌ Сессия создания истекла, начни заново.")
            _wizard_clear(ctx)
            return
        # Move on to the description step (optional).
        wiz["data"]["t_type"] = t_type
        wiz["step"] = "create_t_desc"
        await query.message.reply_text(
            f"Турнир «<b>{name}</b>» [{t_type_label(t_type)}].\n\n"
            f"📝 Напиши описание/правила (или кнопка «Пропустить»):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Пропустить", callback_data="t:create_skip_desc"),
            ]]),
        )
        return

    if data == "t:create_skip_desc":
        wiz = _wizard_get(ctx)
        if not wiz or "name" not in wiz["data"] or "t_type" not in wiz["data"]:
            await query.edit_message_text("❌ Сессия истекла, начни заново.")
            _wizard_clear(ctx)
            return
        ctx.args = [wiz["data"]["name"], wiz["data"]["t_type"]]
        _wizard_clear(ctx)
        await cmd_create_tournament(update, ctx)
        return

    if data.startswith("t:addplayer:"):
        t_id = int(data.split(":", 2)[2])
        _wizard_set(ctx, "addplayer_user", {"tournament_id": t_id})
        await query.message.reply_text(
            "Кого добавить? Напиши <code>@username</code> "
            "(можно сразу с типом: <code>@user вса</code>).",
            parse_mode="HTML",
        )
        return

    if data.startswith("t:setdesc:"):
        _wizard_set(ctx, "set_desc")
        await query.message.reply_text("📝 Напиши описание турнира одним сообщением:")
        return

    if data.startswith("t:setchan:"):
        _wizard_set(ctx, "set_channel")
        await query.message.reply_text(
            "🔗 Какой канал требовать? Напиши <code>@channel</code> или числовой ID. "
            "Бот должен быть в этом канале.",
            parse_mode="HTML",
        )
        return

    if data.startswith("t:start:") or data.startswith("t:playoff:"):
        kind, t_id = data.split(":")[1], int(data.split(":")[2])
        t = get_tournament(t_id)
        if not t:
            await query.message.reply_text("❌ Турнир не найден.")
            return
        ctx.args = [t["tournament_type"]]
        if kind == "start":
            await cmd_start_tournament(update, ctx)
        else:
            await cmd_start_playoff(update, ctx)
        return

    # ── Profile submenu ───────────────────────────────────────────────────
    if data == "p:setnick":
        _wizard_set(ctx, "setnick")
        await query.message.reply_text("🎮 Напиши свой ник в игре одним сообщением:")
        return

    if data == "p:matches":
        await cmd_matches(update, ctx)
        return

    # ── Report submenu ────────────────────────────────────────────────────
    if data == "r:photo":
        await query.message.reply_text(
            "📸 Просто пришли фото скрина матча в этот чат — бот сам всё распознает.",
        )
        return

    if data == "r:manual":
        _wizard_set(ctx, "report_text")
        await query.message.reply_text(
            "⌨️ Напиши: <code>3:2 @opponent</code> (можно добавить <code>вса</code> или <code>ри</code> в конце).",
            parse_mode="HTML",
        )
        return

    # ── Settings submenu ──────────────────────────────────────────────────
    if data == "s:desc":
        _wizard_set(ctx, "set_desc")
        await query.message.reply_text("📝 Напиши новое описание турнира:")
        return

    if data == "s:channel":
        _wizard_set(ctx, "set_channel")
        await query.message.reply_text(
            "🔗 Какой канал требовать (напиши <code>@channel</code> или числовой id)?",
            parse_mode="HTML",
        )
        return

    if data == "s:clear_channel":
        ctx.args = []
        await cmd_clear_channel(update, ctx)
        return

    # ── Tournament settings submenu (button-driven) ───────────────────────
    if data.startswith("ts:"):
        await _handle_tournament_settings_cb(update, ctx, data)
        return

    # ── Team / club tag picker ────────────────────────────────────────────
    if data.startswith("team:"):
        await cb_team_buttons(update, ctx)
        return

    # ── Quote settings menu ───────────────────────────────────────────────
    if data.startswith("qs:"):
        from handlers.quotes import cb_quote_settings
        await cb_quote_settings(update, ctx)
        return

    # ── Jokes inline menu ────────────────────────────────────────────────
    # Single dispatcher for the whole ``j:*`` namespace — settings panel,
    # submenus, and pending-input triggers. See ``cb_jokes_menu`` for the
    # full callback_data schema.
    if data.startswith("j:"):
        from handlers.jokes import cb_jokes_menu
        await cb_jokes_menu(update, ctx)
        return

    # ── AI-analyze inline menu ───────────────────────────────────────────
    # Single dispatcher for the whole ``ai:*`` namespace — preset
    # picker, "change N" submenu, and run trigger. See
    # ``handlers.ai_analysis.cb_ai_menu`` for the full callback_data
    # schema. Per-user-per-chat 1h rate limit + admin bypass live
    # inside the dispatcher itself.
    if data.startswith("ai:"):
        from handlers.ai_analysis import cb_ai_menu
        await cb_ai_menu(update, ctx)
        return

    # ── Top submenu ───────────────────────────────────────────────────────
    if data == "top:elo":
        await cmd_top(update, ctx)
        return
    if data == "top:vsa":
        await cmd_top_vsa(update, ctx)
        return
    if data == "top:ri":
        await cmd_top_ri(update, ctx)
        return
    if data == "top:leaderboard":
        ctx.args = []
        await cmd_leaderboard(update, ctx)
        return
    if data == "top:scorers":
        await cmd_top_scorers(update, ctx)
        return

    # ── Admin submenu ─────────────────────────────────────────────────────
    if data.startswith("a:"):
        if not is_admin(update.effective_user.id):
            await query.message.reply_text("❌ Только для админов.")
            return
        action = data.split(":", 1)[1]
        if action == "ban":
            _wizard_set(ctx, "ban_user")
            await query.message.reply_text(
                "🚫 Кого забанить? Напиши: <code>@user [длительность] [причина]</code>\n"
                "Длительность: <b>24</b> (часы), <b>7d</b>, <b>30m</b>, <b>perm</b>. По умолчанию — 24ч.",
                parse_mode="HTML",
            )
            return
        if action == "unban":
            _wizard_set(ctx, "unban_user")
            await query.message.reply_text("✅ Кого разбанить? Напиши <code>@user</code>", parse_mode="HTML")
            return
        if action == "banned":
            await cmd_banned(update, ctx)
            return
        if action == "elo":
            _wizard_set(ctx, "elo_user")
            await query.message.reply_text(
                "⚖️ Изменить ELO. Напиши: <code>@user +50</code> или <code>@user -100 причина</code>",
                parse_mode="HTML",
            )
            return
        if action == "setelo":
            _wizard_set(ctx, "setelo_user")
            await query.message.reply_text(
                "🎯 Задать ELO. Напиши: <code>@user 200 [причина]</code>",
                parse_mode="HTML",
            )
            return
        if action == "walkover":
            _wizard_set(ctx, "walkover_user")
            await query.message.reply_text(
                "⚠️ Тех. поражение. Напиши <code>@user</code>", parse_mode="HTML",
            )
            return

    if data.startswith("albmpick:") or data.startswith("albmskip:") \
            or data.startswith("albmcancel:"):
        # Album-panel callbacks for multi-screenshot uploads.
        # albmpick:<mgi>:<idx>:<player_id>  — user resolved an ambiguous
        #                                     match by picking opponent.
        # albmskip:<mgi>:<idx>              — user excluded a match.
        # albmcancel:<mgi>                  — user dropped the whole album.
        parts = data.split(":")
        kind = parts[0]
        if kind == "albmcancel":
            mgi_cb = parts[1] if len(parts) > 1 else ""
        else:
            if len(parts) < 3:
                return
            mgi_cb = parts[1]
            try:
                idx_cb = int(parts[2])
            except ValueError:
                return
        album_state = _album_state(ctx, mgi_cb)
        if album_state is None:
            try:
                await query.edit_message_text("❌ Альбом устарел.")
            except TelegramError:
                pass
            return
        matches = album_state.setdefault("matches", [])
        chat_id_cb = (
            update.effective_chat.id if update.effective_chat else None
        )

        if kind == "albmcancel":
            for m in matches:
                if m.get("status") == "ambiguous":
                    m["status"] = "skipped"
            if chat_id_cb is not None:
                await _album_panel_send_or_edit(
                    ctx, chat_id_cb, mgi_cb, album_state,
                )
            return

        if idx_cb < 0 or idx_cb >= len(matches):
            return
        match_entry = matches[idx_cb]
        if match_entry.get("status") != "ambiguous":
            return

        if kind == "albmskip":
            match_entry["status"] = "skipped"
            if chat_id_cb is not None:
                await _album_panel_send_or_edit(
                    ctx, chat_id_cb, mgi_cb, album_state,
                )
            return

        # albmpick — user picked an opponent for this ambiguous match.
        try:
            picked_player_id = int(parts[3])
        except (IndexError, ValueError):
            return
        opponent_pick = get_player_by_id(picked_player_id)
        reporter_pick = _player_from_user(update.effective_user)
        if not opponent_pick or not reporter_pick:
            try:
                await query.edit_message_text("❌ Игрок не найден.")
            except TelegramError:
                pass
            return
        target_tid_pick = match_entry.get("target_tid")
        target_tournament_pick = (
            get_tournament(target_tid_pick)
            if target_tid_pick is not None else None
        )
        # Pull this match's stashed OCR data (goals, screenshot_hash).
        extra_pick = ctx.user_data.get(
            f"ocr_extra_{match_entry.get('file_id')}"
        ) or {}
        try:
            match_id_pick = await _do_report(
                update, ctx,
                reporter=reporter_pick,
                opponent=opponent_pick,
                s1=int(match_entry.get("my_score") or 0),
                s2=int(match_entry.get("opp_score") or 0),
                tournament_type=match_entry.get("t_type"),
                tournament=target_tournament_pick,
                screenshot_hash=extra_pick.get("screenshot_hash"),
                screenshot_file_id=match_entry.get("file_id"),
                ocr_goals=extra_pick.get("goals") or [],
                suppress_result_message=True,
                force_new=True,
            )
        except Exception as e:
            log.exception("album-pick _do_report failed: %s", e)
            match_id_pick = None
        # Drop the temporary stashed result text — album panel owns
        # the user-visible summary.
        ctx.user_data.pop("_last_report_result_text", None)
        if match_id_pick:
            match_entry["status"] = "submitted"
            match_entry["summary"] = (
                f"{mention(reporter_pick['username'])} "
                f"<b>{match_entry.get('my_score')}:"
                f"{match_entry.get('opp_score')}</b> "
                f"{mention(opponent_pick['username'])}"
            )
            match_entry["match_id"] = match_id_pick
            match_entry["auto_confirmed"] = bool(
                target_tournament_pick
                and int(target_tournament_pick.get("auto_confirm") or 0) == 1
            )
        else:
            bail_reason_pick = (
                ctx.user_data.pop("_last_report_error", None)
                or "не удалось записать"
            )
            match_entry["status"] = "error"
            match_entry["error_text"] = (
                f"{match_entry.get('summary', '?')} — {bail_reason_pick}"
            )
        if chat_id_cb is not None:
            await _album_panel_send_or_edit(
                ctx, chat_id_cb, mgi_cb, album_state,
            )
        return

    if data.startswith("ocr_pick:"):
        # ocr_pick:<opp_id>:<my_score>:<opp_score>:<t_type>[:<tid>]
        parts = data.split(":")
        _, opp_id, my_score, opp_score, t_type = parts[:5]
        explicit_tid = (
            int(parts[5])
            if len(parts) > 5 and parts[5] not in ("", "0") and parts[5].isdigit()
            else None
        )
        # 16-char hex token added to identify the exact stash (multi-screenshot fix)
        fid_token_pick = parts[6] if len(parts) > 6 and len(parts[6]) == 16 else None
        user = update.effective_user
        reporter = _player_from_user(user)
        if not reporter:
            await query.edit_message_text("❌ Ты не зарегистрирован.")
            return
        opponent = get_player_by_id(int(opp_id))
        if not opponent:
            await query.edit_message_text("❌ Соперник не найден.")
            return
        explicit_tournament = (
            get_tournament(explicit_tid) if explicit_tid is not None else None
        )
        # Pull screenshot_hash + OCR goals from any of the user's pending
        # OCR stashes. We don't know the exact file_id here, so accept the
        # first match that hasn't been used yet. The cache key embeds the
        # Telegram file_id (``ocr_extra_<file_id>``), so we can recover it
        # from the key and forward the screenshot to admins together with
        # the approve/reject buttons.
        screenshot_hash = None
        screenshot_file_id_pick: str | None = None
        ocr_goals: list[dict] | None = None
        consumed_mgi: str | None = None
        for k, v in list(ctx.user_data.items()):
            if not (k.startswith("ocr_extra_") and isinstance(v, dict)):
                continue
            candidate_fid = k[len("ocr_extra_"):]
            if fid_token_pick is not None:
                # Exact match by token — fixes multi-screenshot stash confusion
                if not candidate_fid or _retry_token_for(candidate_fid) != fid_token_pick:
                    continue
            elif not (v.get("screenshot_hash") or v.get("goals")):
                continue
            screenshot_hash = v.get("screenshot_hash")
            ocr_goals = v.get("goals")
            consumed_mgi = v.get("mgi")
            screenshot_file_id_pick = candidate_fid or None
            ctx.user_data.pop(k, None)
            break
        match_id_done = await _do_report(
            update, ctx,
            reporter=reporter, opponent=opponent,
            s1=int(my_score), s2=int(opp_score),
            tournament_type=t_type,
            tournament=explicit_tournament,
            screenshot_hash=screenshot_hash,
            screenshot_file_id=screenshot_file_id_pick,
            ocr_goals=ocr_goals,
            # Don't post a separate "Результат матча …" message — we'll
            # show the same text by editing the picker message below
            # (one screenshot → exactly one bot message).
            suppress_result_message=True,
        )
        if match_id_done and consumed_mgi:
            # Stamp the album-state so any further screenshot in the
            # same Telegram album merges into THIS match instead of
            # creating a duplicate.
            st = _album_state(ctx, consumed_mgi)
            if st is not None:
                st["match_id"] = match_id_done
                st["p1_id"] = reporter["id"]
                st["p2_id"] = opponent["id"]
        result_text = ctx.user_data.pop("_last_report_result_text", None)
        if not result_text:
            scope = (
                f" [{explicit_tournament['name']}, ID {explicit_tournament['id']}]"
                if explicit_tournament
                else f" [{t_type_label(t_type)}]"
            )
            result_text = (
                f"📨 Отправлено: {mention(reporter['username'])} "
                f"<b>{my_score}:{opp_score}</b> {mention(opponent['username'])}"
                f"{scope}"
            )
        try:
            await query.edit_message_text(result_text, parse_mode="HTML")
        except TelegramError:
            # Editing can fail if the message is already gone; in that
            # case still ensure the user sees confirmation.
            await ctx.bot.send_message(
                update.effective_chat.id, result_text, parse_mode="HTML",
            )
        return

    if data.startswith("ocr_tt:"):
        # ocr_tt:<t_type>  — session stashed in ctx.user_data["ocr_pending"]
        t_type = data.split(":", 1)[1]
        sess = ctx.user_data.get("ocr_pending")
        if not sess:
            await query.edit_message_text("❌ Сессия истекла, пришли скрин ещё раз.")
            return
        opp_text  = sess["opp_text"]
        my_score  = sess["my_score"]
        opp_score = sess["opp_score"]

        candidates = find_players_by_fuzzy_game_nickname(opp_text)
        user = update.effective_user
        reporter = _player_from_user(user)
        candidates = [c for c in candidates if reporter and c[0]["id"] != reporter["id"]]
        opp_safe = html.escape(opp_text or "")
        t_type_safe = html.escape(t_type or "")
        if not candidates:
            await query.edit_message_text(
                f"❌ В базе нет игрока с ником, похожим на «{opp_safe}».\n"
                f"Используй вручную: <code>/report {my_score}:{opp_score} @opponent {t_type_safe}</code>",
                parse_mode="HTML",
            )
            return
        buttons = [[
            InlineKeyboardButton(
                f"@{p['username']} ({p.get('game_nickname','—')}) {int(r*100)}%",
                callback_data=f"ocr_pick:{p['id']}:{my_score}:{opp_score}:{t_type}",
            )
        ] for p, r in candidates[:5]]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="ocr_cancel")])
        await query.edit_message_text(
            f"Турнир: <b>{t_type_label(t_type)}</b>\nСчёт: <b>{my_score}:{opp_score}</b>\n"
            f"Соперник на скрине: <b>{opp_safe}</b>\n\nКто из них?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("confirm:"):
        match_id = int(data.split(":")[1])
        m = get_match(match_id)
        if not m:
            await query.edit_message_text("❌ Матч не найден.")
            return
        if m["status"] != "reported":
            await query.edit_message_text("⚠️ Этот матч уже обработан.")
            return
        user = update.effective_user
        player = _player_from_user(user)
        if not player:
            await query.edit_message_text("❌ Ты не зарегистрирован.")
            return
        if player["id"] == m["reported_by"]:
            await query.edit_message_text("❌ Ты не можешь подтвердить свой же репорт.")
            return
        m = dict(m)
        try:
            await query.edit_message_text(
                "🕐 Подтверждено, отправлено админу на проверку.",
            )
        except TelegramError:
            pass
        await _after_opponent_confirm(update, ctx, m)
        return

    if data.startswith("dispute:"):
        match_id = int(data.split(":")[1])
        update_match(match_id, status="pending")
        await query.edit_message_text(
            "⚠️ <b>Результат оспорен.</b>\n"
            "Обратитесь к организатору турнира для решения спора.",
            parse_mode="HTML",
        )
        return

    # ── Admin match approval ──────────────────────────────────────────────
    if data.startswith("adm_match:"):
        if not is_admin(update.effective_user.id):
            await query.message.reply_text("❌ Только для админов.")
            return
        _, action, mid_str = data.split(":", 2)
        match_id = int(mid_str)
        m = get_match(match_id)

        # Helper: edit text OR caption depending on message type
        async def _adm_edit(text: str, **kwargs):
            try:
                if query.message.photo or query.message.document or query.message.video:
                    await query.edit_message_caption(caption=text, **kwargs)
                else:
                    await query.edit_message_text(text, **kwargs)
            except TelegramError:
                pass

        if not m:
            await _adm_edit("❌ Матч не найден.")
            return
        if m["status"] not in ("awaiting_admin", "reported"):
            await _adm_edit(
                f"⚠️ Матч уже в статусе <b>{m['status']}</b>.",
                parse_mode="HTML",
            )
            return
        if action == "ok":
            await _adm_edit("✅ Засчитываю…")
            await _finalize_match_after_admin(update, ctx, match_id)
            return
        if action == "no":
            update_match(match_id, status="rejected")
            await _adm_edit(f"❌ Матч #{match_id} отклонён.")
            # Notify both players
            mfull = dict(get_match(match_id) or {})
            for pid in (mfull.get("player1_id"), mfull.get("player2_id")):
                if not pid:
                    continue
                p = get_player_by_id(pid)
                if p and p.get("telegram_id"):
                    try:
                        await ctx.bot.send_message(
                            p["telegram_id"],
                            f"❌ Матч #{match_id} отклонён админом. ELO не начислено.\n"
                            f"Подойди к организатору, если не согласен.",
                        )
                    except Exception:
                        pass
            return


# ─────────────────────────────────────────────────────────────────────────────
# /admin_report — admin posts a result for any pair of players
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_ocr_compare(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /ocr_compare — admin-only. Reply to a screenshot with this command;
    bot runs the image through every configured vision model AND the
    local tesseract pipeline, and replies with a side-by-side table.
    """
    user = update.effective_user
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return

    msg = update.message
    target = msg.reply_to_message if msg and msg.reply_to_message else msg
    photos = (target.photo if target else None) or []
    if not photos:
        await send(
            update,
            "Использование: ответом на сообщение со скрином введи "
            "<code>/ocr_compare</code>.",
        )
        return

    file = await ctx.bot.get_file(photos[-1].file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as f:
            jpg = f.read()
        await msg.reply_text("⏳ Прогоняю через все модели…")

        # 1) AI Vision models. The helpers live inside ocr.py — no extra
        #    module needs to be on sys.path.
        from ocr import ai_compare_screenshot, ai_is_available
        try:
            rows = (await asyncio.to_thread(ai_compare_screenshot, jpg)) if ai_is_available() else []
        except Exception as e:
            rows = []
            await send(update, f"⚠️ AI OCR ошибка: <code>{html.escape(str(e))}</code>")

        # 2) Local tesseract baseline.
        from ocr import parse_match_screenshot
        # Disable AI for this single call so we get pure tesseract output.
        prev = os.environ.get("OCR_PROVIDER")
        os.environ["OCR_PROVIDER"] = "tesseract"
        t0 = time.time()
        try:
            tess = await asyncio.to_thread(parse_match_screenshot, jpg)
            tess_dt = time.time() - t0
            tess_row = {
                "model": "tesseract (local)",
                "ok": tess.score1 is not None,
                "score1": tess.score1,
                "score2": tess.score2,
                "team1": tess.team1,
                "team2": tess.team2,
                "league_plate": tess.league_plate,
                "elapsed_s": tess_dt,
                "error": None,
            }
        finally:
            if prev is None:
                os.environ.pop("OCR_PROVIDER", None)
            else:
                os.environ["OCR_PROVIDER"] = prev

        all_rows = rows + [tess_row]

        # Render a compact comparison table.
        lines = ["📊 <b>Сравнение OCR-моделей</b>\n"]
        for r in all_rows:
            name = html.escape(r.get("model", "?"))
            if r.get("ok"):
                s1 = r.get("score1")
                s2 = r.get("score2")
                t1 = html.escape((r.get("team1") or "—")[:32])
                t2 = html.escape((r.get("team2") or "—")[:32])
                lg = html.escape((r.get("league_plate") or "—")[:50])
                dt = r.get("elapsed_s") or 0.0
                lines.append(
                    f"• <b>{name}</b>  ({dt:.1f}s)\n"
                    f"   счёт: <b>{s1}:{s2}</b>\n"
                    f"   t1: <code>{t1}</code>\n"
                    f"   t2: <code>{t2}</code>\n"
                    f"   лига: <i>{lg}</i>"
                )
            else:
                err = html.escape(str(r.get("error") or "")[:160])
                lines.append(f"• <b>{name}</b> — ❌ {err}")
        await send(update, "\n\n".join(lines))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def cmd_test_ocr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /test_ocr — reply to a screenshot to run ONLY the tesseract pipeline
    (no AI) and see the raw OCR results: score, teams, league, goals.
    Useful for debugging tesseract quality on specific screenshots.
    """
    user = update.effective_user
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return

    msg = update.message
    target = msg.reply_to_message if msg and msg.reply_to_message else msg
    photos = (target.photo if target else None) or []
    if not photos:
        await send(
            update,
            "Использование: ответом на сообщение со скрином введи "
            "<code>/test_ocr</code>.",
        )
        return

    file = await ctx.bot.get_file(photos[-1].file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await file.download_to_drive(tmp_path)
        await msg.reply_text("⏳ Запускаю тесеракт…")

        from ocr import parse_match_screenshot

        # Force tesseract-only mode
        prev = os.environ.get("OCR_PROVIDER")
        os.environ["OCR_PROVIDER"] = "tesseract"
        t0 = time.time()
        try:
            res = await asyncio.to_thread(parse_match_screenshot, tmp_path)
            dt = time.time() - t0
        finally:
            if prev is None:
                os.environ.pop("OCR_PROVIDER", None)
            else:
                os.environ["OCR_PROVIDER"] = prev

        lines = ["🔧 <b>Тест Tesseract OCR</b>\n"]
        lines.append(f"⏱ Время: {dt:.1f}s")
        if res.score1 is not None and res.score2 is not None:
            lines.append(f"⚽ Счёт: <b>{res.score1}:{res.score2}</b>")
        else:
            lines.append("⚽ Счёт: ❌ не распознан")
        lines.append(f"👤 Команда 1: <code>{html.escape(res.team1 or '—')}</code>")
        lines.append(f"👤 Команда 2: <code>{html.escape(res.team2 or '—')}</code>")
        lines.append(f"🏆 Лига: <i>{html.escape(res.league_plate or '—')}</i>")
        lines.append(f"⚽ Тип турнира: {res.tournament_type or '—'}")

        if res.goals:
            lines.append(f"\n🥅 Голы ({len(res.goals)}):")
            for g in res.goals:
                name = g.get("name") or "?"
                minute = g.get("minute")
                side = g.get("side") or "?"
                min_str = f"{minute}'" if minute else "?'"
                lines.append(f"  • {min_str} {html.escape(name)} ({side})")

        # Show raw OCR texts for debugging
        if res.raw_texts:
            lines.append("\n📝 <b>Сырой текст:</b>")
            for key, val in res.raw_texts.items():
                if key.startswith("_"):
                    continue
                short = html.escape((val or "")[:80])
                lines.append(f"  <code>{key}</code>: {short}")

        await send(update, "\n".join(lines))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Master text router: menu labels → wizards → feedback → ignore
# ─────────────────────────────────────────────────────────────────────────────

async def _show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=_menu_markup_for(update, update.effective_user.id),
    )


async def _send_tournaments_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    user = update.effective_user
    user_id = user.id if user else None
    actives = get_active_tournaments()
    if not actives:
        if user_id is not None and is_admin(user_id):
            text = "Нет активных турниров. Создай новый кнопкой ниже."
        else:
            text = (
                "Нет активных турниров.\n"
                "Турниры запускают админы — следи за анонсами."
            )
    else:
        lines = ["🏆 <b>Активные турниры</b>\n"]
        any_open = False
        for t in actives:
            members = get_tournament_players(t["id"])
            stage_lower = (t.get("stage") or "groups").lower()
            signup_open = (
                int(t.get("open_signup") or 0)
                and stage_lower in ("groups", "")
                and len(get_tournament_matches(t["id"])) == 0
            )
            badges = []
            if signup_open:
                any_open = True
                badges.append("🙋 запись открыта")
            if int(t.get("groups_only") or 0):
                badges.append("📊 только группы")
            elif int(t.get("bracket_only") or 0):
                badges.append("🏁 только плей-офф")
            badge_str = (" — " + ", ".join(badges)) if badges else ""
            block = [
                f"• <b>{t['name']}</b> [{t_full_label(t)}] — "
                f"{len(members)} игр., этап: <i>{t['stage']}</i>{badge_str}"
            ]
            if t.get("description"):
                block.append(f"  📝 {t['description']}")
            if t.get("required_channel"):
                block.append(f"  🔗 Канал: <b>{t['required_channel']}</b>")
            lines.append("\n".join(block))
        if any_open:
            lines.append(
                "\n💡 Открой <b>📋 Список активных</b> — кнопка "
                "<b>🙋 Записаться</b> рядом с каждым открытым турниром."
            )
        text = "\n\n".join(lines)
    await msg.reply_text(
        text, parse_mode="HTML",
        reply_markup=submenu_tournaments(user_id),
    )


async def _send_profile_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    user = update.effective_user
    p = _player_from_user(user)
    if not p:
        await msg.reply_text(
            "❌ Сначала зарегистрируйся: /register",
            reply_markup=_menu_kb_for(update, user.id),
        )
        return
    # Reuse cmd_profile rendering: just call it via fake ctx (it reads ctx.args)
    ctx.args = []
    await cmd_profile(update, ctx)
    await msg.reply_text("Действия:", reply_markup=submenu_profile())


async def _send_report_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "📨 <b>Репорт матча</b>\n\n"
        "Можно прислать фото скрина — бот распознает счёт и соперника, "
        "или ввести вручную.",
        parse_mode="HTML",
        reply_markup=submenu_report(),
    )


async def _send_top_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "📊 <b>Рейтинг</b>\n\nВыбери, что показать:",
        parse_mode="HTML",
        reply_markup=submenu_top(),
    )


async def _send_settings_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "🔧 <b>Настройки</b>",
        parse_mode="HTML",
        reply_markup=submenu_settings(),
    )


async def _send_quotes_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Open the quote-settings panel for the current chat. The panel
    shows current cadence + quote count + preset buttons. Lazy-imports
    the implementation to avoid the bot ↔ handlers.quotes cycle at
    module load.
    """
    from handlers.quotes import cmd_quote_settings
    await cmd_quote_settings(update, ctx)


async def _send_feedback_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "🐞 <b>Связь с админами</b>\n\n"
        "Жми кнопку и опиши проблему или идею. Можно с фото.",
        parse_mode="HTML",
        reply_markup=submenu_feedback(),
    )


async def _send_admin_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    if not is_admin(update.effective_user.id):
        await msg.reply_text(
            "❌ Только для админов.",
            reply_markup=_menu_kb_for(update, update.effective_user.id),
        )
        return
    await msg.reply_text(
        "👮 <b>Админ-панель</b>",
        parse_mode="HTML",
        reply_markup=submenu_admin(),
    )


MENU_DISPATCH = {
    M_TOURNAMENTS: _send_tournaments_section,
    M_PROFILE:     _send_profile_section,
    M_REPORT:      _send_report_section,
    M_TOP:         _send_top_section,
    M_SETTINGS:    _send_settings_section,
    M_QUOTES:      _send_quotes_section,
    M_FEEDBACK:    _send_feedback_section,
    M_ADMIN:       _send_admin_section,
    M_HELP:        cmd_help,
}


# ── Inline (edit-in-place) versions of menu section handlers ──────────────
# These edit the callback query message instead of sending a new one, so the
# bot doesn't create excessive messages on every button press.

async def _inline_tournaments_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return await _send_tournaments_section(update, ctx)
    user = update.effective_user
    user_id = user.id if user else None
    actives = get_active_tournaments()
    if not actives:
        text = "Нет активных турниров."
    else:
        lines = ["🏆 <b>Активные турниры</b>\n"]
        any_open = False
        for t in actives:
            members = get_tournament_players(t["id"])
            stage_lower = (t.get("stage") or "groups").lower()
            signup_open = (
                int(t.get("open_signup") or 0)
                and stage_lower in ("groups", "")
                and len(get_tournament_matches(t["id"])) == 0
            )
            badges = []
            if signup_open:
                any_open = True
                badges.append("🙋 запись открыта")
            if int(t.get("groups_only") or 0):
                badges.append("📊 только группы")
            elif int(t.get("bracket_only") or 0):
                badges.append("🏁 только плей-офф")
            badge_str = (" — " + ", ".join(badges)) if badges else ""
            block = [
                f"• <b>{t['name']}</b> [{t_full_label(t)}] — "
                f"{len(members)} игр., этап: <i>{t['stage']}</i>{badge_str}"
            ]
            if t.get("description"):
                block.append(f"  📝 {t['description']}")
            lines.append("\n".join(block))
        if any_open:
            lines.append(
                "\n💡 Открой <b>📋 Список активных</b> — кнопка "
                "<b>🙋 Записаться</b> рядом с каждым открытым турниром."
            )
        text = "\n\n".join(lines)
    try:
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=submenu_tournaments(user_id),
        )
    except TelegramError:
        await _send_tournaments_section(update, ctx)


async def _inline_top_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return await _send_top_section(update, ctx)
    try:
        await query.edit_message_text(
            "📊 <b>Рейтинг</b>\n\nВыбери, что показать:",
            parse_mode="HTML",
            reply_markup=submenu_top(),
        )
    except TelegramError:
        await _send_top_section(update, ctx)


async def _inline_report_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return await _send_report_section(update, ctx)
    try:
        await query.edit_message_text(
            "📨 <b>Репорт матча</b>\n\n"
            "Можно прислать фото скрина — бот распознает счёт и соперника, "
            "или ввести вручную.",
            parse_mode="HTML",
            reply_markup=submenu_report(),
        )
    except TelegramError:
        await _send_report_section(update, ctx)


async def _inline_settings_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return await _send_settings_section(update, ctx)
    try:
        await query.edit_message_text(
            "🔧 <b>Настройки</b>",
            parse_mode="HTML",
            reply_markup=submenu_settings(),
        )
    except TelegramError:
        await _send_settings_section(update, ctx)


async def _inline_quotes_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Group inline-menu version: open the quote-settings panel by
    editing the current message in place. Falls back to a fresh
    message if the edit fails (e.g. the original message is gone).
    """
    from handlers.quotes import _quote_settings_text, _quote_settings_kb
    query = update.callback_query
    chat = update.effective_chat
    if not query or chat is None:
        return await _send_quotes_section(update, ctx)
    try:
        await query.edit_message_text(
            _quote_settings_text(chat.id),
            parse_mode="HTML",
            reply_markup=_quote_settings_kb(chat.id),
        )
    except TelegramError:
        await _send_quotes_section(update, ctx)


async def _inline_feedback_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return await _send_feedback_section(update, ctx)
    try:
        await query.edit_message_text(
            "🐞 <b>Связь с админами</b>\n\n"
            "Жми кнопку и опиши проблему или идею. Можно с фото.",
            parse_mode="HTML",
            reply_markup=submenu_feedback(),
        )
    except TelegramError:
        await _send_feedback_section(update, ctx)


async def _inline_admin_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return await _send_admin_section(update, ctx)
    if not is_admin(update.effective_user.id):
        try:
            await query.edit_message_text("❌ Только для админов.")
        except TelegramError:
            pass
        return
    try:
        await query.edit_message_text(
            "👮 <b>Админ-панель</b>",
            parse_mode="HTML",
            reply_markup=submenu_admin(),
        )
    except TelegramError:
        await _send_admin_section(update, ctx)


async def _inline_profile_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return await _send_profile_section(update, ctx)
    user = update.effective_user
    p = _player_from_user(user)
    if not p:
        try:
            await query.edit_message_text("❌ Сначала зарегистрируйся: /register")
        except TelegramError:
            pass
        return
    try:
        await query.edit_message_text(
            "👤 <b>Профиль</b>\n\nВыбери действие:",
            parse_mode="HTML",
            reply_markup=submenu_profile(),
        )
    except TelegramError:
        await _send_profile_section(update, ctx)


async def _inline_help_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return await cmd_help(update, ctx)
    try:
        await query.edit_message_text(
            PLAYER_HELP_TEXT,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
            ]),
        )
    except TelegramError:
        await cmd_help(update, ctx)
        return
    # Also drop the full command reference (COMMANDS.md) as a separate
    # downloadable file so the user has the exhaustive list at hand.
    if query.message:
        await _attach_commands_reference(query.message)


MENU_DISPATCH_INLINE = {
    M_TOURNAMENTS: _inline_tournaments_section,
    M_PROFILE:     _inline_profile_section,
    M_REPORT:      _inline_report_section,
    M_TOP:         _inline_top_section,
    M_SETTINGS:    _inline_settings_section,
    M_QUOTES:      _inline_quotes_section,
    M_FEEDBACK:    _inline_feedback_section,
    M_ADMIN:       _inline_admin_section,
    M_HELP:        _inline_help_section,
}

# Mapping from `gmenu:<key>` callback suffix to the corresponding
# main-menu label, so the inline group-menu reuses MENU_DISPATCH_INLINE.
_GMENU_TO_LABEL = {
    "tournaments": M_TOURNAMENTS,
    "profile":     M_PROFILE,
    "report":      M_REPORT,
    "top":         M_TOP,
    "settings":    M_SETTINGS,
    "quotes":      M_QUOTES,
    "feedback":    M_FEEDBACK,
    "admin":       M_ADMIN,
    "help":        M_HELP,
}


async def _handle_wizard_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, wiz: dict):
    step = wiz["step"]
    data = wiz["data"]
    txt = (update.message.text or "").strip()

    if step == "setnick":
        ctx.args = [txt]
        _wizard_clear(ctx)
        await cmd_setnick(update, ctx)
        return

    if step == "create_t_name":
        if not txt or len(txt) > 50:
            await send(update, "❌ Название от 1 до 50 символов. Попробуй ещё раз.")
            return
        data["name"] = txt
        wiz["step"] = "create_t_type"
        await update.message.reply_text(
            f"Турнир «<b>{txt}</b>». Тип?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚽ ВСА", callback_data="t:create_finish:vsa"),
                InlineKeyboardButton("🎮 РИ", callback_data="t:create_finish:ri"),
            ]]),
        )
        return

    if step == "create_t_desc":
        if not txt:
            await send(update, "❌ Пустое описание. Жми «Пропустить» или напиши текст.")
            return
        if len(txt) > 1000:
            await send(update, "❌ Слишком длинно (макс. 1000 символов).")
            return
        # Create the tournament with description in one go.
        name = data.get("name")
        t_type = data.get("t_type")
        if not name or not t_type:
            _wizard_clear(ctx)
            await send(update, "❌ Сессия истекла, начни заново.")
            return
        ctx.args = [name, t_type]
        _wizard_clear(ctx)
        await cmd_create_tournament(update, ctx)
        # Then attach the description to the just-created tournament
        t = get_active_tournament(tournament_type=t_type)
        if t and t["name"] == name:
            conn = db.get_conn()
            conn.execute("UPDATE tournaments SET description=? WHERE id=?", (txt, t["id"]))
            conn.commit()
            conn.close()
            await send(update, f"📝 Описание сохранено:\n\n{txt}")
        return

    if step == "report_text":
        # Expect "3:2 @opponent [вса|ри]"
        ctx.args = txt.split()
        _wizard_clear(ctx)
        await cmd_report(update, ctx)
        return

    if step == "set_desc":
        if not txt:
            await send(update, "❌ Пустое описание.")
            return
        ctx.args = txt.split()
        _wizard_clear(ctx)
        await cmd_set_description(update, ctx)
        return

    if step == "set_channel":
        if not txt:
            await send(update, "❌ Укажи канал.")
            return
        ctx.args = txt.split()
        _wizard_clear(ctx)
        await cmd_set_channel(update, ctx)
        return

    if step == "ban_user":
        # Expect: "@user [duration] [reason]"
        parts = txt.split()
        if not parts:
            await send(update, "❌ Укажи юзера.")
            return
        ctx.args = parts
        _wizard_clear(ctx)
        await cmd_ban(update, ctx)
        return

    if step == "unban_user":
        ctx.args = txt.split()
        _wizard_clear(ctx)
        await cmd_unban(update, ctx)
        return

    if step == "elo_user":
        ctx.args = txt.split()
        _wizard_clear(ctx)
        await cmd_elo(update, ctx)
        return

    if step == "setelo_user":
        ctx.args = txt.split()
        _wizard_clear(ctx)
        await cmd_setelo(update, ctx)
        return

    if step == "walkover_user":
        ctx.args = txt.split()
        _wizard_clear(ctx)
        await cmd_walkover(update, ctx)
        return

    if step == "addplayer_user":
        ctx.args = txt.split()
        _wizard_clear(ctx)
        await cmd_add_player(update, ctx)
        return

    if step == "set_footer":
        tid = data.get("tid")
        _wizard_clear(ctx)
        if not tid:
            await send(update, "❌ Сессия истекла, начни заново.")
            return
        if not txt:
            await send(update, "❌ Пустой текст. Попробуй ещё раз или /cancel.")
            return
        if len(txt) > 2000:
            await send(update, "❌ Слишком длинный текст (макс. 2000 символов суммарно).")
            return
        from database import get_tournament, update_tournament
        from handlers.common import entities_to_html, format_footer_preview
        import json as _json
        t = get_tournament(tid)
        if not t:
            await send(update, f"❌ Турнир с ID {tid} не найден.")
            return
        # Auto-detect formatting (bold, links, spoilers) from Telegram entities
        msg_ent = update.message.entities or []
        if msg_ent:
            html_text = entities_to_html(txt, msg_ent)
        else:
            html_text = txt
        # Split by | into multiple variants
        variants = [v.strip() for v in html_text.split("|") if v.strip()]
        if not variants:
            await send(update, "❌ Пустой текст после разделения.")
            return
        update_tournament(tid, footer_text=_json.dumps(variants, ensure_ascii=False))
        preview = format_footer_preview(get_tournament(tid))
        await send(
            update,
            f"✅ Подпись для <b>{html.escape(t['name'])}</b> обновлена "
            f"({len(variants)} вар.):\n\n{preview}",
        )
        return

    if step == "add_footer_variant":
        tid = data.get("tid")
        _wizard_clear(ctx)
        if not tid:
            await send(update, "❌ Сессия истекла, начни заново.")
            return
        if not txt:
            await send(update, "❌ Пустой текст. Попробуй ещё раз или /cancel.")
            return
        if len(txt) > 500:
            await send(update, "❌ Один вариант — макс. 500 символов.")
            return
        from database import get_tournament, update_tournament
        from handlers.common import entities_to_html, format_footer_preview
        import json as _json
        t = get_tournament(tid)
        if not t:
            await send(update, f"❌ Турнир с ID {tid} не найден.")
            return
        # Auto-detect formatting from Telegram entities
        msg_ent = update.message.entities or []
        if msg_ent:
            html_text = entities_to_html(txt, msg_ent)
        else:
            html_text = txt
        # Load existing variants and append new one
        raw = (t.get("footer_text") or "").strip()
        variants: list = []
        if raw.startswith("["):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    variants = [str(v) for v in parsed if str(v).strip()]
            except (ValueError, _json.JSONDecodeError):
                pass
        if not variants and raw:
            variants = [raw]
        variants.append(html_text)
        update_tournament(tid, footer_text=_json.dumps(variants, ensure_ascii=False))
        t_fresh = get_tournament(tid)
        preview = format_footer_preview(t_fresh)
        await send(
            update,
            f"✅ Вариант добавлен! Всего вариантов: <b>{len(variants)}</b>\n\n"
            f"{preview}",
        )
        return

    # Unknown step → reset
    _wizard_clear(ctx)
    await send(update, "Что-то пошло не так, начни заново через меню.")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Master text router."""
    if not update.message or not update.message.text:
        return
    # Skip channel posts and other messages without a real authoring
    # user — those would otherwise hit the "❌ Сначала зарегистрируйся"
    # branch (or any other handler that blindly does
    # update.effective_user.id) and spam noise into linked discussion
    # groups every time a channel posts something.
    if update.effective_user is None:
        return
    txt = update.message.text.strip()

    # ── Pending team-tag entry ────────────────────────────────────────────
    # Admin tapped "🏷 Команды → ✏️ <player>" and the next message is
    # expected to be the team name. Consume the message there before
    # any of the menu / wizard / feedback dispatchers pick it up.
    if await handle_pending_team_tag_text(update, ctx):
        return

    # ── Pending tournament creation (name input from /new_tournament) ────
    if await handle_pending_tpl_create_text(update, ctx, txt):
        return

    # ── Pending jokes-menu manual input (interval / context / minmsgs / model) ─
    # Set when an admin taps "✏️ Ввести вручную" in the /jokes panel.
    # Consumed here so the value lands on the right setting before any
    # other text-router branch sees it.
    from handlers.jokes import handle_pending_jokes_text
    if await handle_pending_jokes_text(update, ctx):
        return

    # ── Pending groupname input (from settings panel / set_groupname prompt) ──
    pending_g = ctx.user_data.pop("pending_groupname", None)
    if pending_g:
        from database import update_tournament as _upd_t
        t_id = int(pending_g["tid"])
        new_name = txt.strip()
        if new_name.lower() in ("отмена", "cancel", "отменить"):
            await send(update, "❌ Отменено.")
            return
        if new_name in ("-", "—", "сброс"):
            _upd_t(t_id, group_display_name=None)
            await send(update, f"✅ Имя группы сброшено на «Группа A».")
            return
        if len(new_name) > 64:
            await send(update, "❌ Слишком длинное имя (макс. 64 символа).")
            return
        _upd_t(t_id, group_display_name=new_name)
        await send(
            update,
            f"✅ Имя группы для <b>{html.escape(pending_g['t_name'])}</b>: "
            f"<b>{html.escape(new_name)}</b>",
        )
        return

    # Main menu label tap → exit any wizard, go to that section
    if txt in MENU_LABELS:
        _wizard_clear(ctx)
        ctx.user_data.pop("awaiting_feedback", None)
        ctx.user_data.pop("awaiting_bug", None)
        ctx.user_data.pop("awaiting_fb_reply_to", None)
        handler = MENU_DISPATCH.get(txt)
        if handler:
            await handler(update, ctx)
        return

    # Feedback waiting mode → forward to admins
    if ctx.user_data.get("awaiting_feedback"):
        ctx.user_data.pop("awaiting_feedback", None)
        if not ADMIN_IDS:
            await send(update, "❌ Админы не настроены.")
            return
        delivered = await _send_feedback_to_admins(ctx, update.effective_user, txt)
        if delivered:
            await send(update, f"✅ Спасибо! Доставлено {delivered}/{len(ADMIN_IDS)} админ(ам).")
        else:
            await send(update, "⚠️ Не удалось доставить ни одному админу.")
        return

    # Bug-report waiting mode → forward to admins with 🐞 header.
    if ctx.user_data.get("awaiting_bug"):
        ctx.user_data.pop("awaiting_bug", None)
        if not ADMIN_IDS:
            await send(update, "❌ Админы не настроены.")
            return
        from handlers.leaderboard import _send_bug_to_admins
        delivered = await _send_bug_to_admins(ctx, update.effective_user, txt)
        if delivered:
            await send(
                update,
                f"✅ Багрепорт отправлен ({delivered}/{len(ADMIN_IDS)} "
                f"админ(ам)). Спасибо!",
            )
        else:
            await send(update, "⚠️ Не удалось доставить ни одному админу.")
        return

    # Admin replying to user feedback
    fb_reply_to = ctx.user_data.pop("awaiting_fb_reply_to", None)
    if fb_reply_to is not None:
        admin = update.effective_user
        admin_tag = f"@{admin.username}" if admin.username else f"Админ (id {admin.id})"
        reply_text = (
            f"💬 <b>Ответ от админа</b> ({admin_tag}):\n"
            f"{'─'*30}\n"
            f"{html.escape(txt)}"
        )
        try:
            await ctx.bot.send_message(fb_reply_to, reply_text, parse_mode="HTML")
            await send(update, f"✅ Ответ доставлен пользователю (tg_id={fb_reply_to}).")
        except TelegramError as e:
            await send(update, f"⚠️ Не удалось доставить ответ: {e}")
        return

    # Wizard step → progress
    wiz = _wizard_get(ctx)
    if wiz:
        await _handle_wizard_text(update, ctx, wiz)
        return

    # ── Free-form joke request: "Давай шутку про X" / "шутка про X" ──
    # Last-chance branch BEFORE silent ignore — runs only when nothing
    # else consumed the message (no pending state, no menu label, no
    # wizard step, no feedback/bug reply state). Detector is
    # conservative: a no-topic match requires an imperative verb
    # ("давай/расскажи/сделай/кинь шутку"); a topic-bearing match
    # accepts a bare "шутка про X".
    #
    # Privacy: trigger_joke_request internally checks
    # ``is_jokes_enabled`` and silently no-ops in chats that haven't
    # opted in via ``/jokes_on`` — we don't want the bot volunteering
    # itself in random chats.
    #
    # Quota: also enforced inside trigger_joke_request — non-admins
    # share 5 jokes/chat/day; admins bypass. Same 60-sec anti-spam
    # cooldown as the slash command. No double-consumption: the
    # detector only fires once per message.
    chat = update.effective_chat
    if chat is not None and getattr(chat, "type", None) != "private":
        from handlers.jokes import (
            detect_joke_intent as _detect_joke_intent,
            trigger_joke_request as _trigger_joke_request,
        )
        intent = _detect_joke_intent(txt)
        if intent is not None:
            await _trigger_joke_request(
                update, ctx,
                topic=intent,
                source="freeform",
            )
            return

    # Otherwise ignore (don't react to random chat)


# ─────────────────────────────────────────────────────────────────────────────
# Background job: deadline reminders & walkovers
# ─────────────────────────────────────────────────────────────────────────────

async def job_deadline_check(ctx: ContextTypes.DEFAULT_TYPE):
    soon = get_upcoming_deadline_matches(hours=6)
    for m in soon:
        for pid in [m["player1_id"], m["player2_id"]]:
            p = get_player_by_id(pid)
            if p and p.get("telegram_id"):
                opp_id = m["player2_id"] if pid == m["player1_id"] else m["player1_id"]
                opp = get_player_by_id(opp_id)
                try:
                    await ctx.bot.send_message(
                        p["telegram_id"],
                        f"⏰ <b>Напоминание!</b>\n"
                        f"У тебя осталось ~6 часов для матча против {mention(opp['username'])}.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    overdue = get_overdue_matches()
    for m in overdue:
        try:
            tid = m.get("tournament_id")
            t = get_tournament(tid) if tid else None
            tech_enabled = bool(int(t.get("auto_tech_loss_enabled") or 0)) if t else False

            # If auto-TP is disabled for this tournament, skip entirely —
            # the deadline is informational only; admins handle it manually.
            if not tech_enabled:
                continue

            score_s = (t.get("auto_tech_loss_score") if t else None) or "0:3"
            try:
                a_s, _, b_s = score_s.partition(":")
                s1, s2 = int(a_s), int(b_s)
            except ValueError:
                s1, s2 = 0, 3

            p1 = get_player_by_id(m["player1_id"])
            p2 = get_player_by_id(m["player2_id"])

            # Instead of applying auto-TP immediately, send a confirmation
            # request to the tournament chat (or creator DM). Admin must
            # press a button to actually apply the walkover.
            p1_name = mention(p1['username']) if p1 else str(m['player1_id'])
            p2_name = mention(p2['username']) if p2 else str(m['player2_id'])
            confirm_text = (
                f"⏰ <b>Дедлайн просрочен</b>\n"
                f"Матч #{m['id']}: {p1_name} vs {p2_name}\n"
                f"Турнир: <b>{html.escape(t.get('name', ''))}</b> (ID {tid})\n\n"
                f"Засчитать ТП <b>{s1}:{s2}</b>?"
            )
            confirm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Засчитать ТП",
                        callback_data=f"ts:atl_confirm:{tid}:{m['id']}",
                    ),
                    InlineKeyboardButton(
                        "⏭ Пропустить",
                        callback_data=f"ts:atl_skip:{tid}:{m['id']}",
                    ),
                ]
            ])

            # Determine where to send the confirmation:
            # 1) Tournament's bound chat (if any)
            # 2) Creator's DM
            sent = False
            chat_id = t.get("chat_id")
            if chat_id:
                try:
                    await ctx.bot.send_message(
                        int(chat_id), confirm_text,
                        parse_mode="HTML",
                        reply_markup=confirm_kb,
                    )
                    sent = True
                except Exception:
                    pass

            if not sent:
                # Fallback: send to tournament creator
                creator = get_player_by_id(t.get("created_by")) if t.get("created_by") else None
                if creator and creator.get("telegram_id"):
                    try:
                        await ctx.bot.send_message(
                            creator["telegram_id"], confirm_text,
                            parse_mode="HTML",
                            reply_markup=confirm_kb,
                        )
                        sent = True
                    except Exception:
                        pass

            # Mark the match deadline as "notified" so we don't spam
            # the confirmation every hour. We set a flag in the match
            # metadata by updating deadline to NULL (consumed).
            if sent:
                update_match(m["id"], deadline=None)

        except Exception as e:
            log.warning("Walkover job error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Tournament reminders (DM + chat with escalating cadence)
# ─────────────────────────────────────────────────────────────────────────────

def _list_pending_for_player(pid: int, tid: int) -> list[dict]:
    """
    Real pending/reported matches for ``pid`` in tournament ``tid`` —
    i.e. matches that the player is supposed to play according to the
    tournament's actual draw, NOT phantom matches that may have been
    inserted by ad-hoc ``/report`` / ``/admin_report`` flows between
    players in different groups.

    Filtering rules:
      • Group-stage matches are only listed if BOTH players are in
        the same group of this tournament (per ``tournament_players``).
      • Playoff matches (stage in r16/qf/sf/final) are listed as-is —
        the bracket generator already pairs them correctly.
      • Anything else (e.g. legacy / unknown stage) is dropped to be
        safe.
    """
    conn = db.get_conn()
    # Pull matches + the tournament_players group for both sides so we can
    # filter without a second round-trip. LEFT JOIN to keep the rows where
    # one side might be missing from tournament_players (e.g. legacy data).
    rows = conn.execute(
        """SELECT m.*,
                  tp1.group_name AS p1_group,
                  tp2.group_name AS p2_group
             FROM matches m
        LEFT JOIN tournament_players tp1
               ON tp1.tournament_id = m.tournament_id
              AND tp1.player_id     = m.player1_id
        LEFT JOIN tournament_players tp2
               ON tp2.tournament_id = m.tournament_id
              AND tp2.player_id     = m.player2_id
            WHERE m.tournament_id = ?
              AND m.status IN ('pending','reported')
              AND (m.player1_id = ? OR m.player2_id = ?)
            ORDER BY m.id""",
        (tid, pid, pid),
    ).fetchall()
    conn.close()

    playoff_stages = {"r512", "r256", "r128", "r64", "r32", "r16",
                      "qf", "sf", "final", "third"}
    out: list[dict] = []
    # Dedupe playoff legs by (pair, stage, leg) keeping the highest id —
    # the same defensive strategy as :func:`get_real_tournament_matches`.
    # Without this the reminder shows "vs @opp (×4)" when the bracket was
    # accidentally generated multiple times.
    playoff_seen: dict[tuple, dict] = {}
    for r in rows:
        m = dict(r)
        stage = (m.get("stage") or "").lower()
        if stage in playoff_stages:
            pair = tuple(sorted((m["player1_id"], m["player2_id"])))
            key = (pair, stage, int(m.get("leg") or 1))
            cur = playoff_seen.get(key)
            if cur is None or (m["id"] or 0) > (cur["id"] or 0):
                playoff_seen[key] = m
            continue
        if stage == "group":
            g1 = m.get("p1_group")
            g2 = m.get("p2_group")
            # Only include if both players are in the same group.
            if g1 is not None and g2 is not None and g1 == g2:
                out.append(m)
            # else: phantom match across groups (or one player not in the
            # tournament), drop it from the reminder list.
            continue
        # Unknown stage → drop.
    out.extend(playoff_seen.values())
    return out


def _reminder_should_fire(tid: int, kind: str, period_seconds: int) -> bool:
    """Idempotent throttle: return True iff `period_seconds` have passed
    since the last fire for (tid, kind). Records the fire time on True."""
    if period_seconds <= 0:
        return False
    conn = db.get_conn()
    row = conn.execute(
        "SELECT last_sent_at FROM reminder_log WHERE tournament_id=? AND kind=?",
        (tid, kind),
    ).fetchone()
    now = datetime.utcnow()
    if row:
        try:
            last = datetime.strptime(str(row["last_sent_at"]), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            last = datetime(1970, 1, 1)
        if (now - last).total_seconds() < period_seconds:
            conn.close()
            return False
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    if row:
        conn.execute(
            "UPDATE reminder_log SET last_sent_at=? WHERE tournament_id=? AND kind=?",
            (now_str, tid, kind),
        )
    else:
        conn.execute(
            "INSERT INTO reminder_log (tournament_id, kind, last_sent_at) VALUES (?,?,?)",
            (tid, kind, now_str),
        )
    conn.commit()
    conn.close()
    return True


def _chat_reminder_period_seconds(tournament: dict) -> int:
    """
    Escalating cadence for chat reminders:
      • > 24h before deadline (or no deadline set): every 6 hours
      • 24h .. 6h before deadline:                  every 3 hours
      • < 6h before deadline:                       every 30 minutes
    """
    deadline = tournament.get("deadline_at")
    if not deadline:
        return 6 * 3600
    try:
        dl = datetime.strptime(str(deadline), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 6 * 3600
    remaining = (dl - datetime.utcnow()).total_seconds()
    if remaining <= 0:
        return 30 * 60  # past deadline: keep nagging frequently
    if remaining < 6 * 3600:
        return 30 * 60
    if remaining < 24 * 3600:
        return 3 * 3600
    return 6 * 3600


async def job_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Per-tournament reminder loop. Runs every 15 minutes; for each active
    tournament:
      1. DM each player with pending matches (throttled by `reminder_dm_hours`).
      2. If `reminder_chat_enabled` and `chat_id` is set, post a single
         chat message with the list of pending matches at the cadence
         from `_chat_reminder_period_seconds`.
    """
    try:
        active = db.get_active_tournaments()
    except Exception:
        log.exception("job_reminders: list active tournaments failed")
        return
    for t in active:
        tid = t["id"]
        # Skip tournaments where group stage hasn't started.
        if (t.get("stage") or "") in ("registration", "draft"):
            continue

        # ── DM reminders ──────────────────────────────────────────────
        dm_hours = int(t.get("reminder_dm_hours") or 0)
        if dm_hours > 0:
            tplayers = db.get_tournament_players(tid)
            for tp in tplayers:
                pid = tp["player_id"]
                pending = _list_pending_for_player(pid, tid)
                if not pending:
                    continue
                kind = f"dm:{pid}"
                if not _reminder_should_fire(tid, kind, dm_hours * 3600):
                    continue
                p = db.get_player_by_id(pid)
                if not p or not p.get("telegram_id"):
                    continue
                # Group pending matches by stage + opponent so a 2-leg
                # tie shows up as "vs @opp (×2)" instead of two separate
                # bullets. Group/playoff are listed in separate buckets
                # so a player still in groups doesn't see a confusing
                # mix once playoff fixtures are generated.
                buckets: dict[str, dict[int, dict]] = {"group": {}, "playoff": {}}
                for m in pending:
                    opp_pid = (
                        m["player2_id"] if m["player1_id"] == pid else m["player1_id"]
                    )
                    stage = (m.get("stage") or "").lower()
                    bucket = "playoff" if stage in ("r512", "r256", "r128", "r64", "r32", "r16", "qf", "sf", "final", "third") else "group"
                    entry = buckets[bucket].setdefault(opp_pid, {"count": 0, "stage": stage})
                    entry["count"] += 1
                    # Keep the most "advanced" stage label for playoff bucket.
                    entry["stage"] = stage

                opp_lines: list[str] = []
                stage_pretty = {"r512": "1/256", "r256": "1/128", "r128": "1/64",
                                "r64": "1/32", "r32": "1/16", "r16": "1/8",
                                "qf": "1/4", "sf": "1/2", "final": "Финал",
                                "third": "3-е место"}
                # Render opponent name with their per-tournament team
                # tag (when configured) so DM reminders read like
                # "vs phoenileo - Германия (@Phoenileo)".
                from handlers._helpers import format_player_with_tag_html
                # Group bucket first (since groups are played before playoff).
                for label, bucket_name in (("Группа", "group"), ("Плей-офф", "playoff")):
                    bucket = buckets[bucket_name]
                    if not bucket:
                        continue
                    opp_lines.append(f"<b>{label}:</b>")
                    for opp_pid, entry in list(bucket.items())[:6]:
                        opp = db.get_player_by_id(opp_pid)
                        if not opp:
                            continue
                        try:
                            opp_tag = db.get_tournament_player_tag(tid, opp_pid)
                        except Exception:
                            opp_tag = ""
                        opp_label = (
                            format_player_with_tag_html(opp, opp_tag)
                            if opp_tag
                            else mention(opp["username"])
                        )
                        suffix = ""
                        if entry["count"] > 1:
                            suffix = f" (×{entry['count']})"
                        if bucket_name == "playoff":
                            stg = stage_pretty.get(entry["stage"], entry["stage"])
                            opp_lines.append(
                                f"  • {stg} vs {opp_label}{suffix}"
                            )
                        else:
                            opp_lines.append(
                                f"  • vs {opp_label}{suffix}"
                            )

                if not opp_lines:
                    continue
                t_label = html.escape(t["name"])
                try:
                    await ctx.bot.send_message(
                        p["telegram_id"],
                        f"⏰ Напоминание: несыгранные матчи в турнире "
                        f"<b>{t_label}</b>\n"
                        + "\n".join(opp_lines)
                        + "\n\nИграй и присылай скрин в чат турнира "
                        f"(или используй <code>/report</code>).",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        # ── Chat reminders ────────────────────────────────────────────
        if int(t.get("reminder_chat_enabled") or 0) == 1 and t.get("chat_id"):
            period = _chat_reminder_period_seconds(t)
            if not _reminder_should_fire(tid, "chat", period):
                continue
            # Build a compact summary of all pending matches — but only
            # *real* fixtures: same-group group matches and playoff
            # bracket matches. Phantom cross-group matches created by
            # ad-hoc /report flows are filtered out via the same JOIN
            # that DM reminders use.
            conn = db.get_conn()
            rows = conn.execute(
                """SELECT m.*,
                          tp1.group_name AS p1_group,
                          tp2.group_name AS p2_group
                     FROM matches m
                LEFT JOIN tournament_players tp1
                       ON tp1.tournament_id = m.tournament_id
                      AND tp1.player_id     = m.player1_id
                LEFT JOIN tournament_players tp2
                       ON tp2.tournament_id = m.tournament_id
                      AND tp2.player_id     = m.player2_id
                    WHERE m.tournament_id = ?
                      AND m.status IN ('pending','reported')
                    ORDER BY m.stage, m.id""",
                (tid,),
            ).fetchall()
            conn.close()
            playoff_stages = {"r512", "r256", "r128", "r64", "r32", "r16",
                              "qf", "sf", "final", "third"}
            real_rows = []
            for r in rows:
                rd = dict(r)
                stage = (rd.get("stage") or "").lower()
                if stage in playoff_stages:
                    real_rows.append(rd)
                elif stage == "group":
                    g1, g2 = rd.get("p1_group"), rd.get("p2_group")
                    if g1 is not None and g2 is not None and g1 == g2:
                        real_rows.append(rd)
            if not real_rows:
                continue
            from handlers._helpers import format_player_with_tag_html  # local

            def _name_for(player_row, pid: int) -> str:
                if not player_row:
                    return mention("?")
                try:
                    tt = db.get_tournament_player_tag(tid, int(pid))
                except Exception:
                    tt = ""
                return (
                    format_player_with_tag_html(player_row, tt)
                    if tt
                    else mention(player_row.get("username") or "?")
                )

            lines = [f"⏰ <b>{html.escape(t['name'])}</b> — несыгранные матчи:"]
            for rd in real_rows[:15]:
                p1 = db.get_player_by_id(rd["player1_id"])
                p2 = db.get_player_by_id(rd["player2_id"])
                if not p1 or not p2:
                    continue
                stage = rd.get("stage") or "group"
                stage_lbl = {"group": "🔵", "qf": "1/4", "sf": "1/2",
                             "final": "🏆", "r16": "1/8",
                             "r32": "1/16", "r64": "1/32",
                             "r128": "1/64", "r256": "1/128",
                             "r512": "1/256"}.get(stage, stage)
                leg = rd.get("leg") or 1
                leg_str = f" L{leg}" if leg and leg != 1 else ""
                lines.append(
                    f"  {stage_lbl}{leg_str} "
                    f"{_name_for(p1, rd['player1_id'])} vs "
                    f"{_name_for(p2, rd['player2_id'])}"
                )
            if t.get("deadline_at"):
                lines.append(
                    f"\n📅 Дедлайн: <b>{_fmt_minute_local(t['deadline_at'])} "
                    f"{_tz_label()}</b>"
                )
            # Append footer for chat reminders
            from handlers.common import get_random_footer, FOOTER_CTX_REMINDER
            _rem_footer = get_random_footer(t, FOOTER_CTX_REMINDER)
            if _rem_footer:
                lines.append(_rem_footer)
            try:
                await ctx.bot.send_message(
                    int(t["chat_id"]),
                    "\n".join(lines),
                    parse_mode="HTML",
                )
            except Exception:
                log.exception("chat reminder failed for tournament %s", tid)

        # ── Signup-phase reminders ────────────────────────────────────
        # Posted in the bound chat while the tournament is still
        # accepting registrations (open_signup=1, no matches generated
        # yet). Cadence is verbatim from ``signup_reminder_minutes`` —
        # admins set it via /set_signup_reminder; 0 disables. The
        # message includes the admin-supplied ``signup_link`` (URL or
        # free-form text) when set, plus the same inline "🙋
        # Записаться" button used by /tournaments so users can
        # one-tap join right from the reminder.
        try:
            interval_min = int(t.get("signup_reminder_minutes") or 0)
        except (TypeError, ValueError):
            interval_min = 0
        if interval_min > 0 and t.get("chat_id"):
            stage_lower = (t.get("stage") or "groups").lower()
            try:
                signups_open = (
                    int(t.get("open_signup") or 0) == 1
                    and stage_lower in ("groups", "")
                    and len(db.get_tournament_matches(tid)) == 0
                )
            except Exception:
                signups_open = False
            if signups_open and _reminder_should_fire(
                tid, "signup_chat", interval_min * 60,
            ):
                try:
                    members = db.get_tournament_players(tid)
                except Exception:
                    members = []
                lines: list[str] = [
                    f"🙋 <b>Запись на «{html.escape(t['name'])}» открыта!</b>",
                    f"Уже записалось: <b>{len(members)}</b> игроков.",
                ]
                deadline_raw = t.get("signup_deadline_at")
                if deadline_raw:
                    try:
                        lines.append(
                            f"📅 Дедлайн записи: "
                            f"<b>{_fmt_minute_local(deadline_raw)} "
                            f"{_tz_label()}</b>"
                        )
                    except Exception:
                        # Fall back to the raw value rather than dropping
                        # the deadline silently.
                        lines.append(
                            f"📅 Дедлайн записи: <b>{html.escape(str(deadline_raw))}</b>"
                        )
                link_raw = (t.get("signup_link") or "").strip()
                if link_raw:
                    # The link / free-form text might contain HTML
                    # special chars (admins paste random URLs and prose
                    # into it). We escape it so a stray '&' or '<'
                    # doesn't break the parse_mode='HTML' send.
                    lines.append(
                        f"🔗 Где регаться: {html.escape(link_raw)}"
                    )
                else:
                    lines.append(
                        "Жми кнопку ниже, чтобы записаться 👇"
                    )
                # Inline "🙋 Записаться" button — same callback as in
                # /tournaments so the existing self-signup flow handles
                # the rest. (Players already in the lobby see the
                # "🚪 Покинуть турнир" variant only inside /tournaments —
                # in the chat reminder we always show the join button;
                # the callback itself is idempotent and replies "уже
                # записан" if they already are.)
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🙋 Записаться",
                        callback_data=f"t:join:{tid}",
                    ),
                ]])
                # Append per-tournament chat-reminder footer for parity
                # with the match-reminder branch above.
                try:
                    from handlers.common import (
                        FOOTER_CTX_REMINDER,
                        get_random_footer,
                    )
                    _signup_footer = get_random_footer(t, FOOTER_CTX_REMINDER)
                    if _signup_footer:
                        lines.append(_signup_footer)
                except Exception:
                    pass
                try:
                    await ctx.bot.send_message(
                        int(t["chat_id"]),
                        "\n".join(lines),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception:
                    log.exception(
                        "signup reminder failed for tournament %s", tid,
                    )


# ─────────────────────────────────────────────────────────────────────────────
# Background job: per-chat random-quote rotation
# ─────────────────────────────────────────────────────────────────────────────


async def job_quotes(ctx: ContextTypes.DEFAULT_TYPE):
    """Per-chat quote loop — runs every ~5 min.

    For each chat in ``chat_settings`` with
    ``quote_interval_minutes > 0``, post a random quote drawn from
    that chat's submissions if at least ``quote_interval_minutes``
    have passed since ``last_quote_at``. Throttled per chat (not
    per bot) so multiple chats can have completely different
    cadences without interfering.

    Quiet hours (``quiet_start_hour`` .. ``quiet_end_hour`` in the
    operator's display TZ — МСК by default) suppress posting at
    night. Defaults are 23..12 — quotes only fire between noon and
    11 PM МСК. Admins can override per chat via
    ``set_chat_quote_quiet_hours`` (settings panel button).

    Silently skips chats that have no quotes yet (admin enabled the
    cadence but nobody has used ``/quote`` yet) — no spam, no error.
    """
    from datetime import datetime as _dt
    from handlers.common import _display_tz  # local: avoid cycle at import
    try:
        chats = db.list_chats_with_quote_interval()
    except Exception:
        log.exception("job_quotes: list chats failed")
        return
    now = _dt.utcnow()
    # Local-time hour for quiet-hour gating. _display_tz() respects
    # the operator's BOT_DISPLAY_TZ env (МСК by default).
    try:
        tz = _display_tz()
        local_hour = _dt.now(tz).hour
    except Exception:
        local_hour = now.hour  # fall back to UTC if TZ resolution fails
    for c in chats:
        chat_id = c.get("chat_id")
        try:
            interval_min = int(c.get("quote_interval_minutes") or 0)
        except (TypeError, ValueError):
            interval_min = 0
        if interval_min <= 0 or not chat_id:
            continue

        # Quiet-hour gate. start == end disables the window (24/7
        # quotes). Wrap-around windows (e.g. 23..12) are handled via
        # the OR variant — "in window if hour >= start OR hour < end".
        try:
            qs = int(c.get("quiet_start_hour") if c.get("quiet_start_hour") is not None else 23)
            qe = int(c.get("quiet_end_hour") if c.get("quiet_end_hour") is not None else 12)
        except (TypeError, ValueError):
            qs, qe = 23, 12
        if qs != qe:
            in_quiet = (
                (qs < qe and qs <= local_hour < qe)
                or (qs > qe and (local_hour >= qs or local_hour < qe))
            )
            if in_quiet:
                continue

        last_raw = c.get("last_quote_at")
        last = None
        if last_raw:
            try:
                last = _dt.strptime(str(last_raw), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                last = None
        if last and (now - last).total_seconds() < interval_min * 60:
            continue
        # Pick a random quote and post it. Skip silently when no
        # submissions exist yet.
        try:
            q = db.random_quote_for_chat(chat_id)
        except Exception:
            log.exception("job_quotes: random_quote_for_chat(%s)", chat_id)
            continue
        if not q:
            # Mark as sent anyway so we don't re-poll the same empty
            # chat on every tick.
            try:
                db.mark_chat_quote_sent(chat_id)
            except Exception:
                pass
            continue
        from handlers.quotes import _format_quote, _format_voice_caption
        voice_fid = q.get("voice_file_id")
        if voice_fid:
            try:
                await ctx.bot.send_voice(
                    int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
                    voice=voice_fid,
                    caption=_format_voice_caption(q.get("author") or ""),
                    parse_mode="HTML",
                )
            except Exception:
                log.exception("job_quotes: send_voice to %s failed", chat_id)
                continue
        else:
            body = _format_quote(q.get("text") or "", q.get("author") or "")
            try:
                await ctx.bot.send_message(
                    int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
                    body,
                    parse_mode="HTML",
                )
            except Exception:
                log.exception("job_quotes: send to %s failed", chat_id)
                continue
        try:
            db.mark_chat_quote_sent(chat_id)
        except Exception:
            log.exception("job_quotes: mark sent for %s failed", chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    init_db()
    # Telegram default per-request timeout in python-telegram-bot is 5 s.
    # That is too tight for /standings and /playoff PNGs once a custom
    # ``/set_tournament_bg`` background is attached: rendering becomes a
    # 1–3 MB photo and the upload regularly blows past 5 s on hosted
    # workers, surfacing as ``reply_photo failed: Timed out — falling
    # back to text``. Bump every relevant timeout so big PNGs upload
    # reliably; ``media_write_timeout`` is the one that actually governs
    # photo/document uploads.
    app = (
        Application.builder()
        .token(TOKEN)
        .defaults(Defaults(
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        ))
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .media_write_timeout(180)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("ver", cmd_version))
    app.add_handler(CommandHandler("build", cmd_version))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("id", cmd_myid))
    app.add_handler(CommandHandler("whoami", cmd_myid))
    app.add_handler(CommandHandler("hide_keyboard", cmd_hide_keyboard))
    app.add_handler(CommandHandler("hidekeyboard", cmd_hide_keyboard))
    app.add_handler(CommandHandler("hide_menu", cmd_hide_keyboard))
    app.add_handler(CommandHandler("hidemenu", cmd_hide_keyboard))
    app.add_handler(CommandHandler("show_keyboard", cmd_show_keyboard))
    app.add_handler(CommandHandler("showkeyboard", cmd_show_keyboard))
    app.add_handler(CommandHandler("show_menu", cmd_show_keyboard))
    app.add_handler(CommandHandler("showmenu", cmd_show_keyboard))
    # /keyboard — inline-toggle (works even when no ReplyKeyboard is active)
    app.add_handler(CommandHandler("keyboard", cmd_keyboard))
    app.add_handler(CommandHandler("kb", cmd_keyboard))
    app.add_handler(CommandHandler("menu_toggle", cmd_keyboard))
    app.add_handler(CommandHandler("toggle_menu", cmd_keyboard))
    # Custom group-friendly start command
    app.add_handler(CommandHandler("elodrak", cmd_start))
    # ── Quotes (per-chat user-submitted quotations + scheduled rotation) ──
    from handlers.quotes import (
        cmd_quote, cmd_quotes, cmd_delete_quote, cmd_set_quote_interval,
        cmd_quote_settings, cmd_quote_help,
    )
    app.add_handler(CommandHandler("quote", cmd_quote))
    app.add_handler(CommandHandler("addquote", cmd_quote))
    app.add_handler(CommandHandler("add_quote", cmd_quote))
    # Common typo from the user — keep it as a friendly alias
    app.add_handler(CommandHandler("quto", cmd_quote))
    app.add_handler(CommandHandler("quotes", cmd_quotes))
    app.add_handler(CommandHandler("listquotes", cmd_quotes))
    app.add_handler(CommandHandler("list_quotes", cmd_quotes))
    app.add_handler(CommandHandler("delquote", cmd_delete_quote))
    app.add_handler(CommandHandler("del_quote", cmd_delete_quote))
    app.add_handler(CommandHandler("delete_quote", cmd_delete_quote))
    app.add_handler(CommandHandler("set_quote_interval", cmd_set_quote_interval))
    app.add_handler(CommandHandler("setquoteinterval", cmd_set_quote_interval))
    app.add_handler(CommandHandler("quote_interval", cmd_set_quote_interval))
    app.add_handler(CommandHandler("quoteinterval", cmd_set_quote_interval))
    # Inline button menu for the cadence + quick stats.
    app.add_handler(CommandHandler("quote_settings", cmd_quote_settings))
    app.add_handler(CommandHandler("quotesettings", cmd_quote_settings))
    app.add_handler(CommandHandler("quote_menu", cmd_quote_settings))
    app.add_handler(CommandHandler("quotemenu", cmd_quote_settings))
    app.add_handler(CommandHandler("citaty", cmd_quote_settings))
    # Full guide.
    app.add_handler(CommandHandler("quote_help", cmd_quote_help))
    app.add_handler(CommandHandler("quotehelp", cmd_quote_help))
    app.add_handler(CommandHandler("quote_guide", cmd_quote_help))
    app.add_handler(CommandHandler("quoteguide", cmd_quote_help))

    # ── Auto-jokes (LLM-generated jokes from recent chat context) ──
    # Lazy logger lives at group=-1 so it always runs first and never
    # blocks the master text router below at group=0. Inside the
    # logger we double-check that ``jokes_enabled=true`` for the chat
    # before persisting anything (privacy).
    #
    # UX: as of 2026-06 there are only THREE slash commands —
    # ``/jokes`` (admin: open inline panel), ``/joke`` (admin: fire
    # one now), ``/jokes_history`` (public: read-only list). Every
    # other setting (on/off/interval/mode/context/minmsgs/model/
    # clear-log) lives behind buttons in the panel; the ``j:*``
    # CallbackQueryHandler dispatches to ``cb_jokes_menu``.
    from handlers.jokes import (
        log_chat_message as _log_chat_message,
        cmd_joke as _cmd_joke,
        cmd_jokes_menu as _cmd_jokes_menu,
        cmd_jokes_history as _cmd_jokes_history,
        job_jokes as _job_jokes,
        on_message_reaction as _on_message_reaction,
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            _log_chat_message,
        ),
        group=-1,
    )
    # Reaction feedback loop: jokes the chat reacts to (👍/❤️/🔥 etc.)
    # bump that joke's score and become style exemplars for future
    # generations; the handler covers BOTH per-user
    # (``update.message_reaction``, requires bot to be admin) and
    # chat-wide aggregate (``update.message_reaction_count``) events.
    # ``run_polling`` must subscribe to those update kinds —
    # ``allowed_updates=Update.ALL_TYPES`` below opts in to all of
    # them, including reactions, edited messages, and chat-member
    # updates. See handlers.jokes.on_message_reaction for the
    # routing logic.
    app.add_handler(MessageReactionHandler(_on_message_reaction))
    app.add_handler(CommandHandler("joke", _cmd_joke))
    app.add_handler(CommandHandler("jokes", _cmd_jokes_menu))
    app.add_handler(CommandHandler("jokes_menu", _cmd_jokes_menu))
    app.add_handler(CommandHandler("jokesmenu", _cmd_jokes_menu))
    app.add_handler(CommandHandler("jokes_history", _cmd_jokes_history))
    app.add_handler(CommandHandler("jokeshistory", _cmd_jokes_history))

    # ── AI chat-analysis (/analyze [N]) ──────────────────────────────
    # Reuses the same chat_messages buffer as auto-jokes — the
    # message logger above already persists rows when
    # ``analyze_enabled=true`` for the chat (independent of
    # jokes_enabled). UX: ``/analyze`` opens an inline preset-picker
    # (Сводка / Планы / Темы / Настроение); a click runs the LLM
    # over the last N messages and posts a ≤300-char result inside
    # an expandable blockquote. Per-user-per-chat 1h rate limit
    # (admins bypass) lives inside the callback dispatcher.
    from handlers.ai_analysis import (
        cmd_analyze as _cmd_analyze,
        cmd_analyze_on as _cmd_analyze_on,
        cmd_analyze_off as _cmd_analyze_off,
    )
    app.add_handler(CommandHandler("analyze", _cmd_analyze))
    app.add_handler(CommandHandler("analyse", _cmd_analyze))  # BR-EN alias
    app.add_handler(CommandHandler("analyze_on", _cmd_analyze_on))
    app.add_handler(CommandHandler("analyzeon", _cmd_analyze_on))
    app.add_handler(CommandHandler("analyze_off", _cmd_analyze_off))
    app.add_handler(CommandHandler("analyzeoff", _cmd_analyze_off))
    # NB: Telegram only allows ASCII / digits / underscore in command
    # names, so the Russian-only aliases (/цитаты, /баг) had to go —
    # ``CommandHandler('цитаты', ...)`` raises at startup. Users
    # reach the same panel via the inline "💬 Цитаты" main-menu
    # button or the Latin aliases above.
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("admincmd", cmd_admincmd))
    app.add_handler(CommandHandler("admin_cmd", cmd_admincmd))
    app.add_handler(CommandHandler("adminhelp", cmd_admincmd))
    app.add_handler(CommandHandler("admin_help", cmd_admincmd))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("setnick", cmd_setnick))
    app.add_handler(CommandHandler("set_nick", cmd_setnick))
    app.add_handler(CommandHandler("create_tournament", cmd_create_tournament))
    app.add_handler(CommandHandler("tournaments", cmd_tournaments))
    # Template-based tournament creation
    from handlers.templates import (
        cmd_new_tournament, cmd_templates, cmd_save_template,
        cmd_delete_template, cmd_draw_manual, cmd_draw_random,
    )
    app.add_handler(CommandHandler("new_tournament", cmd_new_tournament))
    app.add_handler(CommandHandler("newtournament", cmd_new_tournament))
    app.add_handler(CommandHandler("templates", cmd_templates))
    app.add_handler(CommandHandler("save_template", cmd_save_template))
    app.add_handler(CommandHandler("savetemplate", cmd_save_template))
    app.add_handler(CommandHandler("delete_template", cmd_delete_template))
    app.add_handler(CommandHandler("deletetemplate", cmd_delete_template))
    app.add_handler(CommandHandler("draw_manual", cmd_draw_manual))
    app.add_handler(CommandHandler("drawmanual", cmd_draw_manual))
    app.add_handler(CommandHandler("draw_random", cmd_draw_random))
    app.add_handler(CommandHandler("drawrandom", cmd_draw_random))
    app.add_handler(CommandHandler("add_player", cmd_add_player))
    app.add_handler(CommandHandler("list_players", cmd_list_players))
    app.add_handler(CommandHandler("listplayers", cmd_list_players))
    app.add_handler(CommandHandler("replace_player", cmd_replace_player))
    app.add_handler(CommandHandler("replaceplayer", cmd_replace_player))
    app.add_handler(CommandHandler("start_tournament", cmd_start_tournament))
    app.add_handler(CommandHandler("redraw_groups", cmd_redraw_groups))
    app.add_handler(CommandHandler("redrawgroups", cmd_redraw_groups))
    app.add_handler(CommandHandler("set_group", cmd_set_group))
    app.add_handler(CommandHandler("setgroup", cmd_set_group))
    app.add_handler(CommandHandler("clear_groups", cmd_clear_groups))
    app.add_handler(CommandHandler("cleargroups", cmd_clear_groups))
    app.add_handler(CommandHandler("ocr_compare", cmd_ocr_compare))
    app.add_handler(CommandHandler("ocrcompare", cmd_ocr_compare))
    app.add_handler(CommandHandler("test_ocr", cmd_test_ocr))
    app.add_handler(CommandHandler("testocr", cmd_test_ocr))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("dispute", cmd_dispute))
    app.add_handler(CommandHandler("table", cmd_table))
    # /standings is a more conventional alias for the same handler.
    app.add_handler(CommandHandler("standings", cmd_table))
    # Text-only flavours: same data, no PNG rendering.
    app.add_handler(CommandHandler("table_text", cmd_table_text))
    app.add_handler(CommandHandler("tabletext", cmd_table_text))
    app.add_handler(CommandHandler("standings_text", cmd_table_text))
    app.add_handler(CommandHandler("standingstext", cmd_table_text))
    app.add_handler(CommandHandler("playoff", cmd_playoff))
    app.add_handler(CommandHandler("bracket", cmd_playoff))
    app.add_handler(CommandHandler("playoff_text", cmd_playoff_text))
    app.add_handler(CommandHandler("playofftext", cmd_playoff_text))
    app.add_handler(CommandHandler("bracket_text", cmd_playoff_text))
    app.add_handler(CommandHandler("brackettext", cmd_playoff_text))
    app.add_handler(CommandHandler("close_groups", cmd_close_groups))
    app.add_handler(CommandHandler("start_playoff", cmd_start_playoff))
    app.add_handler(CommandHandler("redraw_playoff", cmd_redraw_playoff))
    app.add_handler(CommandHandler("redrawplayoff", cmd_redraw_playoff))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("top_vsa", cmd_top_vsa))
    app.add_handler(CommandHandler("top_ri", cmd_top_ri))
    app.add_handler(CommandHandler("top_scorers", cmd_top_scorers))
    app.add_handler(CommandHandler("topscorers", cmd_top_scorers))
    app.add_handler(CommandHandler("scorers", cmd_top_scorers))
    app.add_handler(CommandHandler("bombardiry", cmd_top_scorers))
    app.add_handler(CommandHandler("tablebomb", cmd_table_bomb))
    app.add_handler(CommandHandler("table_bomb", cmd_table_bomb))
    app.add_handler(CommandHandler("bomb", cmd_table_bomb))
    app.add_handler(CommandHandler("bombardiry_t", cmd_table_bomb))
    app.add_handler(CommandHandler("bombs", cmd_table_bomb))
    app.add_handler(CommandHandler("admin_addgoal", cmd_admin_addgoal))
    app.add_handler(CommandHandler("adminaddgoal", cmd_admin_addgoal))
    app.add_handler(CommandHandler("addgoal", cmd_admin_addgoal))
    app.add_handler(CommandHandler("admin_delgoal", cmd_admin_delgoal))
    app.add_handler(CommandHandler("admindelgoal", cmd_admin_delgoal))
    app.add_handler(CommandHandler("delgoal", cmd_admin_delgoal))
    app.add_handler(CommandHandler("admin_setgoalauthor", cmd_admin_setgoalauthor))
    app.add_handler(CommandHandler("adminsetgoalauthor", cmd_admin_setgoalauthor))
    app.add_handler(CommandHandler("setgoalauthor", cmd_admin_setgoalauthor))
    app.add_handler(CommandHandler("admin_reassign_goal", cmd_admin_setgoalauthor))
    app.add_handler(CommandHandler("reassign_goal", cmd_admin_setgoalauthor))
    app.add_handler(CommandHandler("reassigngoal", cmd_admin_setgoalauthor))
    app.add_handler(CommandHandler("admin_setgoalname", cmd_admin_setgoalname))
    app.add_handler(CommandHandler("adminsetgoalname", cmd_admin_setgoalname))
    app.add_handler(CommandHandler("setgoalname", cmd_admin_setgoalname))
    app.add_handler(CommandHandler("admin_renamegoal", cmd_admin_setgoalname))
    app.add_handler(CommandHandler("renamegoal", cmd_admin_setgoalname))
    app.add_handler(CommandHandler("admin_matchgoals", cmd_admin_matchgoals))
    app.add_handler(CommandHandler("adminmatchgoals", cmd_admin_matchgoals))
    app.add_handler(CommandHandler("matchgoals", cmd_admin_matchgoals))
    app.add_handler(CommandHandler("admin_addplayer_late", cmd_admin_addplayer_late))
    app.add_handler(CommandHandler("adminaddplayerlate", cmd_admin_addplayer_late))
    app.add_handler(CommandHandler("addplayer_late", cmd_admin_addplayer_late))
    app.add_handler(CommandHandler("addplayerlate", cmd_admin_addplayer_late))
    app.add_handler(CommandHandler("joinlate", cmd_admin_addplayer_late))
    # User-friendly aliases for participant management mid-tournament.
    app.add_handler(CommandHandler("add_participant", cmd_admin_addplayer_late))
    app.add_handler(CommandHandler("addparticipant",  cmd_admin_addplayer_late))
    app.add_handler(CommandHandler("add_player",      cmd_admin_addplayer_late))
    app.add_handler(CommandHandler("addplayer",       cmd_admin_addplayer_late))
    app.add_handler(CommandHandler("edit_goals", cmd_edit_goals))
    app.add_handler(CommandHandler("editgoals", cmd_edit_goals))
    app.add_handler(CommandHandler("set_goals", cmd_edit_goals))
    app.add_handler(CommandHandler("setgoals", cmd_edit_goals))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("matches", cmd_matches))
    app.add_handler(CommandHandler("walkover", cmd_walkover))
    app.add_handler(CommandHandler("walkover_match", cmd_walkover_match))
    app.add_handler(CommandHandler("walkovermatch", cmd_walkover_match))
    app.add_handler(CommandHandler("walkover_all", cmd_walkover_all))
    app.add_handler(CommandHandler("walkoverall", cmd_walkover_all))
    app.add_handler(CommandHandler("tp_all", cmd_walkover_all))
    app.add_handler(CommandHandler("tpall", cmd_walkover_all))
    app.add_handler(CommandHandler("tp", cmd_walkover))
    app.add_handler(CommandHandler("tech_nil_all", cmd_tech_nil_all))
    app.add_handler(CommandHandler("technilall", cmd_tech_nil_all))
    app.add_handler(CommandHandler("tn_all", cmd_tech_nil_all))
    app.add_handler(CommandHandler("tnall", cmd_tech_nil_all))
    app.add_handler(CommandHandler("promote", cmd_promote))
    app.add_handler(CommandHandler("force_advance", cmd_promote))
    app.add_handler(CommandHandler("forceadvance", cmd_promote))
    app.add_handler(CommandHandler("advance_player", cmd_promote))
    app.add_handler(CommandHandler("advanceplayer", cmd_promote))
    app.add_handler(CommandHandler("po_stage_config", cmd_po_stage_config))
    app.add_handler(CommandHandler("po_stage", cmd_po_stage_config))
    app.add_handler(CommandHandler("po_format", cmd_po_stage_config))
    app.add_handler(CommandHandler("playoff_stage_config", cmd_po_stage_config))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("pending_matches", cmd_pending))
    app.add_handler(CommandHandler("pendingmatches", cmd_pending))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("tlog", cmd_audit))
    app.add_handler(CommandHandler("auditlog", cmd_audit))
    app.add_handler(CommandHandler("audit_log", cmd_audit))
    app.add_handler(CommandHandler("prune_phantoms", cmd_prune_phantoms))
    app.add_handler(CommandHandler("prunephantoms", cmd_prune_phantoms))
    app.add_handler(CommandHandler("clean_phantoms", cmd_prune_phantoms))
    app.add_handler(CommandHandler("fill_missing_matches", cmd_fill_missing_matches))
    app.add_handler(CommandHandler("fillmissing", cmd_fill_missing_matches))
    app.add_handler(CommandHandler("fill_matches", cmd_fill_missing_matches))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("banned", cmd_banned))
    app.add_handler(CommandHandler("elo", cmd_elo))
    app.add_handler(CommandHandler("setelo", cmd_setelo))
    app.add_handler(CommandHandler("set_description", cmd_set_description))
    app.add_handler(CommandHandler("set_channel", cmd_set_channel))
    app.add_handler(CommandHandler("clear_channel", cmd_clear_channel))
    app.add_handler(CommandHandler("set_footer", cmd_set_footer))
    app.add_handler(CommandHandler("setfooter", cmd_set_footer))
    app.add_handler(CommandHandler("clear_footer", cmd_clear_footer))
    app.add_handler(CommandHandler("clearfooter", cmd_clear_footer))
    app.add_handler(CommandHandler("set_groupname", cmd_set_groupname))
    app.add_handler(CommandHandler("setgroupname", cmd_set_groupname))
    app.add_handler(CommandHandler("clear_groupname", cmd_clear_groupname))
    app.add_handler(CommandHandler("cleargroupname", cmd_clear_groupname))
    app.add_handler(CommandHandler("bind_tournament", cmd_bind_tournament))
    app.add_handler(CommandHandler("unbind_tournament", cmd_unbind_tournament))
    app.add_handler(CommandHandler("grant_admin", cmd_grant_admin))
    app.add_handler(CommandHandler("grantadmin", cmd_grant_admin))
    app.add_handler(CommandHandler("revoke_admin", cmd_revoke_admin))
    app.add_handler(CommandHandler("revokeadmin", cmd_revoke_admin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("owner", cmd_set_owner))
    app.add_handler(CommandHandler("setowner", cmd_set_owner))
    app.add_handler(CommandHandler("set_owner", cmd_set_owner))
    app.add_handler(CommandHandler("revoke_owner", cmd_revoke_owner))
    app.add_handler(CommandHandler("revokeowner", cmd_revoke_owner))
    app.add_handler(CommandHandler("owners", cmd_owners))
    app.add_handler(CommandHandler("add_tadmin", cmd_add_tadmin))
    app.add_handler(CommandHandler("addtadmin",  cmd_add_tadmin))
    app.add_handler(CommandHandler("remove_tadmin", cmd_remove_tadmin))
    app.add_handler(CommandHandler("removetadmin", cmd_remove_tadmin))
    app.add_handler(CommandHandler("tadmins", cmd_tadmins))
    app.add_handler(CommandHandler("give_owner", cmd_give_owner))
    app.add_handler(CommandHandler("giveowner", cmd_give_owner))
    app.add_handler(CommandHandler("transfer_owner", cmd_give_owner))
    app.add_handler(CommandHandler("transferowner", cmd_give_owner))
    # v12 additions
    app.add_handler(CommandHandler("h2h", cmd_h2h))
    app.add_handler(CommandHandler("vs",  cmd_h2h))
    app.add_handler(CommandHandler("my_deadlines", cmd_my_deadlines))
    app.add_handler(CommandHandler("mydeadlines",  cmd_my_deadlines))
    app.add_handler(CommandHandler("deadlines",    cmd_my_deadlines))
    app.add_handler(CommandHandler("tlog", cmd_tlog))
    app.add_handler(CommandHandler("tournament_log", cmd_tlog))
    app.add_handler(CommandHandler("tournamentlog",  cmd_tlog))
    app.add_handler(CommandHandler("playoff_preview", cmd_playoff_preview))
    app.add_handler(CommandHandler("playoffpreview",  cmd_playoff_preview))
    app.add_handler(CommandHandler("preview_playoff", cmd_playoff_preview))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CommandHandler("kick_player", cmd_withdraw))
    app.add_handler(CommandHandler("kickplayer",  cmd_withdraw))
    app.add_handler(CommandHandler("remove_participant", cmd_withdraw))
    app.add_handler(CommandHandler("removeparticipant",  cmd_withdraw))
    app.add_handler(CommandHandler("remove_player",      cmd_withdraw))
    app.add_handler(CommandHandler("removeplayer",       cmd_withdraw))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("announce",  cmd_broadcast))
    # v13 additions
    app.add_handler(CommandHandler("set_tournament_bg", cmd_set_tournament_bg))
    app.add_handler(CommandHandler("settournamentbg",   cmd_set_tournament_bg))
    app.add_handler(CommandHandler("set_bg",            cmd_set_tournament_bg))
    app.add_handler(CommandHandler("tournament_bg",     cmd_set_tournament_bg))
    app.add_handler(CommandHandler("clear_tournament_bg", cmd_clear_tournament_bg))
    app.add_handler(CommandHandler("cleartournamentbg",   cmd_clear_tournament_bg))
    app.add_handler(CommandHandler("clear_bg",            cmd_clear_tournament_bg))
    app.add_handler(CallbackQueryHandler(cb_advance_now, pattern=r"^adv_now:"))
    app.add_handler(CommandHandler("admin_setnick", cmd_admin_setnick))
    app.add_handler(CommandHandler("adminsetnick", cmd_admin_setnick))
    app.add_handler(CommandHandler("setnick_for", cmd_admin_setnick))
    app.add_handler(CommandHandler("setnickfor", cmd_admin_setnick))
    app.add_handler(CommandHandler("admin_addplayer", cmd_admin_addplayer))
    app.add_handler(CommandHandler("adminaddplayer", cmd_admin_addplayer))
    app.add_handler(CommandHandler("admin_add_player", cmd_admin_addplayer))
    app.add_handler(CommandHandler("addplayer", cmd_admin_addplayer))
    app.add_handler(CommandHandler("relink_player", cmd_relink_player))
    app.add_handler(CommandHandler("relinkplayer", cmd_relink_player))
    app.add_handler(CommandHandler("relink", cmd_relink_player))
    app.add_handler(CommandHandler("merge_player", cmd_relink_player))
    app.add_handler(CommandHandler("mergeplayer", cmd_relink_player))
    app.add_handler(CommandHandler("cl_spawn_cups", cmd_cl_spawn_cups))
    app.add_handler(CommandHandler("clspawncups", cmd_cl_spawn_cups))
    app.add_handler(CommandHandler("spawn_cups", cmd_cl_spawn_cups))
    app.add_handler(CommandHandler("recompute_standings", cmd_recompute_standings))
    app.add_handler(CommandHandler("recompute_table", cmd_recompute_standings))
    app.add_handler(CommandHandler("recalc_standings", cmd_recompute_standings))
    app.add_handler(CommandHandler("recalc_table", cmd_recompute_standings))
    app.add_handler(CommandHandler("finish_tournament", cmd_finish_tournament))
    app.add_handler(CommandHandler("finishtournament", cmd_finish_tournament))
    app.add_handler(CommandHandler("end_tournament", cmd_finish_tournament))
    app.add_handler(CommandHandler("close_tournament", cmd_finish_tournament))
    app.add_handler(CommandHandler("tournament_summary", cmd_tournament_summary))
    app.add_handler(CommandHandler("tournamentsummary", cmd_tournament_summary))
    app.add_handler(CommandHandler("summary", cmd_tournament_summary))
    app.add_handler(CommandHandler("tournament_report", cmd_tournament_summary))
    app.add_handler(CommandHandler("tournamentreport", cmd_tournament_summary))
    app.add_handler(CommandHandler("svodka", cmd_tournament_summary))
    app.add_handler(CommandHandler("past_tournaments", cmd_past_tournaments))
    app.add_handler(CommandHandler("pasttournaments", cmd_past_tournaments))
    app.add_handler(CommandHandler("finished_tournaments", cmd_past_tournaments))
    app.add_handler(CommandHandler("finishedtournaments", cmd_past_tournaments))
    app.add_handler(CommandHandler("tournaments_finished", cmd_past_tournaments))
    app.add_handler(CommandHandler("itogi", cmd_past_tournaments))
    app.add_handler(CommandHandler("results", cmd_past_tournaments))
    app.add_handler(CommandHandler("compare_tournaments", cmd_compare_tournaments))
    app.add_handler(CommandHandler("comparetournaments", cmd_compare_tournaments))
    app.add_handler(CommandHandler("compare", cmd_compare_tournaments))
    app.add_handler(CommandHandler("sravnenie", cmd_compare_tournaments))
    app.add_handler(CommandHandler("all_tournaments", cmd_compare_tournaments))
    app.add_handler(CommandHandler("alltournaments", cmd_compare_tournaments))
    # Tours (rounds) commands
    app.add_handler(CommandHandler("tours", cmd_tours))
    app.add_handler(CommandHandler("tourstext", cmd_tourstext))
    app.add_handler(CommandHandler("next_tour", cmd_next_tour))
    app.add_handler(CommandHandler("nexttour", cmd_next_tour))
    app.add_handler(CommandHandler("regen_tours", cmd_regen_tours))
    app.add_handler(CommandHandler("regentours", cmd_regen_tours))
    app.add_handler(CommandHandler("regen", cmd_regen_tours))
    app.add_handler(CommandHandler("drop_ghost_matches", cmd_drop_ghost_matches))
    app.add_handler(CommandHandler("dropghostmatches", cmd_drop_ghost_matches))
    app.add_handler(CommandHandler("dropghosts", cmd_drop_ghost_matches))
    app.add_handler(CommandHandler("repair_tour_numbers", cmd_repair_tour_numbers))
    app.add_handler(CommandHandler("repairtournumbers", cmd_repair_tour_numbers))
    app.add_handler(CommandHandler("repair_tours", cmd_repair_tour_numbers))
    app.add_handler(CommandHandler("post_tours", cmd_post_tours))
    app.add_handler(CommandHandler("posttours", cmd_post_tours))
    app.add_handler(CommandHandler("announce_tours", cmd_post_tours))
    app.add_handler(CommandHandler("edit_announce", cmd_edit_announce))
    app.add_handler(CommandHandler("editannounce", cmd_edit_announce))
    app.add_handler(CommandHandler("tour_diag", cmd_tour_diag))
    app.add_handler(CommandHandler("tourdiag", cmd_tour_diag))
    app.add_handler(CommandHandler("export_db", cmd_export_db))
    app.add_handler(CommandHandler("exportdb", cmd_export_db))
    app.add_handler(CommandHandler("backup_db", cmd_export_db))
    app.add_handler(CommandHandler("import_db", cmd_import_db))
    app.add_handler(CommandHandler("importdb", cmd_import_db))
    app.add_handler(CommandHandler("restore_db", cmd_import_db))
    # Bot source export — packs the entire repo into a ZIP and sends it.
    # Aliases keep parity with the DB export commands.
    app.add_handler(CommandHandler("export_bot", cmd_export_bot))
    app.add_handler(CommandHandler("exportbot", cmd_export_bot))
    app.add_handler(CommandHandler("backup_bot", cmd_export_bot))
    # Document handler for the actual restore upload — must run BEFORE
    # the generic photo handler since DB exports come as ZIPs.
    app.add_handler(MessageHandler(filters.Document.ALL, on_db_import_document))
    app.add_handler(CommandHandler("simulate", cmd_simulate))
    app.add_handler(CommandHandler("autosim", cmd_simulate))
    app.add_handler(CommandHandler("admin_report", cmd_admin_report))
    app.add_handler(CommandHandler("adminreport", cmd_admin_report))
    app.add_handler(CommandHandler("force_report", cmd_admin_report))
    app.add_handler(CommandHandler("admin_photo", cmd_admin_photo))
    app.add_handler(CommandHandler("adminphoto", cmd_admin_photo))
    app.add_handler(CommandHandler("photo_report", cmd_admin_photo))
    app.add_handler(CommandHandler("photoreport", cmd_admin_photo))
    app.add_handler(CommandHandler("reocr", cmd_reocr))
    app.add_handler(CommandHandler("re_ocr", cmd_reocr))
    app.add_handler(CommandHandler("tessocr", cmd_reocr))
    app.add_handler(CommandHandler("tess_ocr", cmd_reocr))
    app.add_handler(CommandHandler("edit_match", cmd_edit_match))
    app.add_handler(CommandHandler("editmatch", cmd_edit_match))
    app.add_handler(CommandHandler("delete_match", cmd_delete_match))
    app.add_handler(CommandHandler("deletematch", cmd_delete_match))
    app.add_handler(CommandHandler("force_confirm", cmd_force_confirm))
    app.add_handler(CommandHandler("forceconfirm", cmd_force_confirm))
    app.add_handler(CommandHandler("tmatches", cmd_tmatches))
    app.add_handler(CommandHandler("t_matches", cmd_tmatches))
    app.add_handler(CommandHandler("tournament_matches", cmd_tmatches))
    app.add_handler(CommandHandler("award_points", cmd_award_points))
    app.add_handler(CommandHandler("awardpoints", cmd_award_points))
    app.add_handler(CommandHandler("give_points", cmd_award_points))
    app.add_handler(CommandHandler("givepoints", cmd_award_points))
    # Player titles / awards (free-form, multiple per player)
    app.add_handler(CommandHandler("award", cmd_award))
    app.add_handler(CommandHandler("title", cmd_award))
    app.add_handler(CommandHandler("give_title", cmd_award))
    app.add_handler(CommandHandler("givetitle", cmd_award))
    app.add_handler(CommandHandler("revoke_award", cmd_revoke_award))
    app.add_handler(CommandHandler("revokeaward", cmd_revoke_award))
    app.add_handler(CommandHandler("revoke_title", cmd_revoke_award))
    app.add_handler(CommandHandler("revoketitle", cmd_revoke_award))
    app.add_handler(CommandHandler("awards", cmd_awards))
    app.add_handler(CommandHandler("titles", cmd_awards))
    app.add_handler(CommandHandler("my_titles", cmd_awards))
    app.add_handler(CommandHandler("mytitles", cmd_awards))
    app.add_handler(CommandHandler("set_playoff_slots", cmd_set_playoff_slots))
    app.add_handler(CommandHandler("setplayoffslots", cmd_set_playoff_slots))
    app.add_handler(CommandHandler("playoff_slots", cmd_set_playoff_slots))
    app.add_handler(CommandHandler("set_series_length", cmd_set_series_length))
    app.add_handler(CommandHandler("setserieslength", cmd_set_series_length))
    app.add_handler(CommandHandler("series_length", cmd_set_series_length))
    app.add_handler(CommandHandler("set_auto_confirm", cmd_set_auto_confirm))
    app.add_handler(CommandHandler("setautoconfirm", cmd_set_auto_confirm))
    app.add_handler(CommandHandler("auto_confirm", cmd_set_auto_confirm))
    app.add_handler(CommandHandler("set_third_place", cmd_set_third_place))
    app.add_handler(CommandHandler("setthirdplace", cmd_set_third_place))
    app.add_handler(CommandHandler("third_place", cmd_set_third_place))
    app.add_handler(CommandHandler("skip_third_place", cmd_skip_third_place))
    app.add_handler(CommandHandler("skipthirdplace", cmd_skip_third_place))
    app.add_handler(CommandHandler("cancel_third_place", cmd_skip_third_place))
    app.add_handler(CommandHandler("cancelthirdplace", cmd_skip_third_place))
    app.add_handler(CommandHandler("skip_bronze", cmd_skip_third_place))
    # Per-tournament team / club tags (admin command + self-service).
    app.add_handler(CommandHandler("set_team", cmd_set_team))
    app.add_handler(CommandHandler("setteam", cmd_set_team))
    app.add_handler(CommandHandler("team_set", cmd_set_team))
    app.add_handler(CommandHandler("myteam", cmd_myteam))
    app.add_handler(CommandHandler("my_team", cmd_myteam))
    app.add_handler(CommandHandler("setmyteam", cmd_myteam))
    app.add_handler(CommandHandler("set_penalties", cmd_set_penalties))
    app.add_handler(CommandHandler("setpenalties", cmd_set_penalties))
    app.add_handler(CommandHandler("penalties", cmd_set_penalties))
    app.add_handler(CommandHandler("set_pen", cmd_set_penalties))
    app.add_handler(CommandHandler("setpen", cmd_set_penalties))
    app.add_handler(CommandHandler("set_overlay", cmd_set_overlay))
    app.add_handler(CommandHandler("setoverlay", cmd_set_overlay))
    app.add_handler(CommandHandler("overlay", cmd_set_overlay))
    app.add_handler(CommandHandler("set_transparency", cmd_set_overlay))
    app.add_handler(CommandHandler("settransparency", cmd_set_overlay))
    app.add_handler(CommandHandler("set_row_alpha", cmd_set_row_alpha))
    app.add_handler(CommandHandler("setrowalpha", cmd_set_row_alpha))
    app.add_handler(CommandHandler("row_alpha", cmd_set_row_alpha))
    app.add_handler(CommandHandler("rowalpha", cmd_set_row_alpha))
    app.add_handler(CommandHandler("set_matches_per_pair", cmd_set_matches_per_pair))
    app.add_handler(CommandHandler("setmatchesperpair", cmd_set_matches_per_pair))
    app.add_handler(CommandHandler("matches_per_pair", cmd_set_matches_per_pair))
    app.add_handler(CommandHandler("set_reminders", cmd_set_reminders))
    app.add_handler(CommandHandler("setreminders", cmd_set_reminders))
    app.add_handler(CommandHandler("reminders", cmd_set_reminders))
    # Signup-phase reminders (admin sets cadence in minutes + optional link).
    app.add_handler(CommandHandler("set_signup_reminder", cmd_set_signup_reminder))
    app.add_handler(CommandHandler("setsignupreminder", cmd_set_signup_reminder))
    app.add_handler(CommandHandler("signup_reminder", cmd_set_signup_reminder))
    app.add_handler(CommandHandler("signupreminder", cmd_set_signup_reminder))
    app.add_handler(CommandHandler("set_signup_link", cmd_set_signup_link))
    app.add_handler(CommandHandler("setsignuplink", cmd_set_signup_link))
    app.add_handler(CommandHandler("signup_link", cmd_set_signup_link))
    app.add_handler(CommandHandler("clear_signup_link", cmd_clear_signup_link))
    app.add_handler(CommandHandler("clearsignuplink", cmd_clear_signup_link))
    app.add_handler(CommandHandler("set_signup_deadline", cmd_set_signup_deadline))
    app.add_handler(CommandHandler("setsignupdeadline", cmd_set_signup_deadline))
    app.add_handler(CommandHandler("signup_deadline", cmd_set_signup_deadline))
    app.add_handler(CommandHandler("clear_signup_deadline", cmd_clear_signup_deadline))
    app.add_handler(CommandHandler("clearsignupdeadline", cmd_clear_signup_deadline))
    # Technical draw and per-match deadline editor.
    app.add_handler(CommandHandler("tech_draw", cmd_tech_draw))
    app.add_handler(CommandHandler("techdraw",  cmd_tech_draw))
    app.add_handler(CommandHandler("draw",      cmd_tech_draw))
    app.add_handler(CommandHandler("td",        cmd_tech_draw))
    app.add_handler(CommandHandler("set_deadline", cmd_set_match_deadline))
    app.add_handler(CommandHandler("setdeadline",  cmd_set_match_deadline))
    app.add_handler(CommandHandler("set_dd",       cmd_set_match_deadline))
    app.add_handler(CommandHandler("setdd",        cmd_set_match_deadline))
    app.add_handler(CommandHandler("dd",           cmd_set_match_deadline))
    app.add_handler(CommandHandler("change_deadline", cmd_set_match_deadline))
    app.add_handler(CommandHandler("changedeadline",  cmd_set_match_deadline))
    app.add_handler(CommandHandler("advance", cmd_advance_playoff))
    app.add_handler(CommandHandler("advance_playoff", cmd_advance_playoff))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    # /bug — sister command for bug reports (separate "awaiting" slot,
    # admins see "🐞 BUG REPORT" header for easy triage).
    app.add_handler(CommandHandler("bug", cmd_bug))
    app.add_handler(CommandHandler("bugreport", cmd_bug))
    app.add_handler(CommandHandler("bug_report", cmd_bug))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # ── Champions / Hall of Fame ──────────────────────────────────────
    # User-facing browser of past tournament winners (parsed from the
    # @gvardiolPlay channel dump). Admin-only sub-tools live alongside:
    # /alias to map free-form names to registered players, and
    # /import_champions to bulk-load data/champions_parsed.json.
    from handlers.champions import (
        cmd_champions as _cmd_champions,
        cmd_champion as _cmd_champion,
        cmd_alias as _cmd_alias,
        cmd_import_champions as _cmd_import_champions,
        cmd_rename_champion as _cmd_rename_champion,
        cmd_add_trophy as _cmd_add_trophy,
        cmd_list_trophies as _cmd_list_trophies,
        cmd_remove_trophy as _cmd_remove_trophy,
    )
    app.add_handler(CommandHandler("champions",        _cmd_champions))
    app.add_handler(CommandHandler("champs",           _cmd_champions))
    app.add_handler(CommandHandler("hall_of_fame",     _cmd_champions))
    app.add_handler(CommandHandler("halloffame",       _cmd_champions))
    app.add_handler(CommandHandler("zalslavy",         _cmd_champions))
    app.add_handler(CommandHandler("champion",         _cmd_champion))
    app.add_handler(CommandHandler("champ",            _cmd_champion))
    app.add_handler(CommandHandler("alias",            _cmd_alias))
    app.add_handler(CommandHandler("aliases",          _cmd_alias))
    app.add_handler(CommandHandler("import_champions", _cmd_import_champions))
    app.add_handler(CommandHandler("importchampions",  _cmd_import_champions))
    # Manual Hall-of-Fame curation (admin-only). See handlers/champions.py
    # for the rationale on why these live next to the importer.
    app.add_handler(CommandHandler("rename_champion",  _cmd_rename_champion))
    app.add_handler(CommandHandler("renamechampion",   _cmd_rename_champion))
    app.add_handler(CommandHandler("champ_setnick",    _cmd_rename_champion))
    app.add_handler(CommandHandler("champion_setnick", _cmd_rename_champion))
    app.add_handler(CommandHandler("add_trophy",       _cmd_add_trophy))
    app.add_handler(CommandHandler("addtrophy",        _cmd_add_trophy))
    app.add_handler(CommandHandler("trophy_add",       _cmd_add_trophy))
    app.add_handler(CommandHandler("list_trophies",    _cmd_list_trophies))
    app.add_handler(CommandHandler("listtrophies",     _cmd_list_trophies))
    app.add_handler(CommandHandler("trophies",         _cmd_list_trophies))
    app.add_handler(CommandHandler("remove_trophy",    _cmd_remove_trophy))
    app.add_handler(CommandHandler("removetrophy",     _cmd_remove_trophy))
    app.add_handler(CommandHandler("del_trophy",       _cmd_remove_trophy))
    app.add_handler(CommandHandler("trophy_remove",    _cmd_remove_trophy))

    # Photo handler — auto OCR match screenshot (also handles photo feedback)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Master text router (menu labels + wizards + feedback)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Deadline job — every hour
    app.job_queue.run_repeating(job_deadline_check, interval=3600, first=60)
    # Reminder loop — every 15 minutes; per-tournament cadence is decided
    # inside `job_reminders` (DM hours + escalating chat schedule).
    app.job_queue.run_repeating(job_reminders, interval=15 * 60, first=120)
    # Quote loop — every 5 minutes; per-chat cadence (admin-set) is
    # checked inside ``job_quotes``. Idle chats no-op cheaply.
    app.job_queue.run_repeating(job_quotes, interval=5 * 60, first=300)
    # Auto-jokes loop — same cadence, gated per-chat by interval +
    # min_msgs_since_last inside ``handlers.jokes.job_jokes``. First
    # run is offset 30s after job_quotes so the two LLM-using loops
    # don't fire in the same tick on bot restart.
    app.job_queue.run_repeating(_job_jokes, interval=5 * 60, first=330)
    app.add_error_handler(error_handler)

    log.info("Bot started ✅")
    # ``allowed_updates=Update.ALL_TYPES`` opts in to every update
    # type Telegram supports (including ``message_reaction`` and
    # ``message_reaction_count``, which are NOT in the default
    # subscription list). Without this the joke feedback loop would
    # never fire because the bot wouldn't be told about reactions.
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Last-resort error handler: log + tell the user it broke."""
    log.exception("Unhandled exception while processing update", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await ctx.bot.send_message(
                update.effective_chat.id,
                "⚠️ Произошла ошибка при обработке. Попробуй ещё раз или /start.",
            )
    except Exception:
        pass


if __name__ == "__main__":
    main()
