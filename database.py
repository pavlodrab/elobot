"""
Data layer for the league bot.

Backend selection is delegated to ``db_backend.py``:
- If ``DATABASE_URL`` env var is set (Postgres URL) → Postgres (Railway).
- Otherwise → SQLite at ``$DB_PATH`` (default ``./league.db``).

The module keeps the legacy SQLite-flavored SQL, and the backend wrapper
translates it transparently for Postgres.
"""
from __future__ import annotations

import os

from db_backend import (
    DB_PATH,
    IS_POSTGRES,
    Conn,
    column_exists as _column_exists_be,
    connect as _connect,
    table_exists as _table_exists_be,
)


# Initial ELO for newly registered players (changed from 1000 to 0).
INITIAL_ELO = 0

# Re-export for callers that still import from `database` directly.
__all__ = [
    "DB_PATH",
    "INITIAL_ELO",
    "IS_POSTGRES",
    "get_conn",
    "init_db",
    "upsert_player",
    "get_player",
    "get_player_by_id",
    "get_player_by_telegram_id",
    "update_player_username",
    "merge_players",
    "get_player_by_game_nickname",
    "find_players_by_fuzzy_game_nickname",
    "get_all_players_by_elo_field",
    "get_all_players",
    "update_player_stats",
    "set_game_nickname",
    "ban_player",
    "unban_player",
    "is_player_banned",
    "adjust_player_elo",
    "set_player_elo",
    "create_tournament",
    "get_tournament",
    "get_active_tournament",
    "get_active_tournaments",
    "update_tournament",
    "add_player_to_tournament",
    "is_player_in_tournament",
    "remove_player_from_tournament",
    "get_tournament_players",
    "update_tournament_player",
    "get_tournament_player_tag",
    "create_match",
    "get_match",
    "find_match_by_screenshot_hash",
    "record_processed_screenshot",
    "get_processed_screenshot",
    "count_confirmed_matches_between",
    "get_pending_match",
    "update_match",
    "get_tournament_matches",
    "get_real_tournament_matches",
    "replace_tournament_player",
    "get_player_matches",
    "get_overdue_matches",
    "get_upcoming_deadline_matches",
    "get_tournament_elo",
    "upsert_tournament_elo",
    "get_tournament_leaderboard",
    "get_top_scorers_by_side_for_tournament",
    "get_footballer_scorers_for_tournament",
    "add_match_goal",
    "delete_match_goal",
    "get_match_goal",
    "update_match_goal_author",
    "late_join_tournament_group",
    "set_tournament_chat",
    "unset_tournament_chat",
    "get_tournament_by_chat",
    "find_tournaments_by_name_substring",
    "grant_bot_admin",
    "revoke_bot_admin",
    "is_bot_admin_db",
    "list_bot_admins",
    "add_tournament_admin",
    "remove_tournament_admin",
    "is_tournament_admin",
    "list_tournament_admins",
    "list_tournament_admin_for_user",
    "get_existing_group_match",
    "count_group_matches_for_pair",
    "list_tournament_audit_log",
    "get_recent_tournaments",
    "get_audit_distinct_actors",
    # Player titles / awards
    "add_player_title",
    "list_player_titles",
    "remove_player_title_by_text",
    "player_title_strings",
    # Tours (rounds)
    "create_tournament_tour",
    "get_tournament_tours",
    "get_tour_matches",
    "get_next_tour_number",
    "set_current_tour",
    "is_tour_complete",
    "set_tour_status",
    # Quotes & per-chat settings
    "add_quote",
    "list_quotes",
    "get_quote",
    "random_quote_for_chat",
    "delete_quote",
    "get_chat_settings",
    "set_chat_quote_interval",
    "set_chat_quote_quiet_hours",
    "mark_chat_quote_sent",
    "list_chats_with_quote_interval",
]


def get_conn() -> Conn:
    """Open a new database connection (same API as before)."""
    return _connect()


def _column_exists(conn: Conn, table: str, column: str) -> bool:
    return _column_exists_be(conn, table, column)


# ── Schema & migrations ──────────────────────────────────────────────────────

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute(
        f"""
        CREATE TABLE IF NOT EXISTS players (
            id              INTEGER PRIMARY KEY,
            username        TEXT NOT NULL UNIQUE,
            telegram_id     INTEGER UNIQUE,
            game_nickname   TEXT,
            elo             REAL    DEFAULT {INITIAL_ELO},
            elo_vsa         REAL    DEFAULT {INITIAL_ELO},
            elo_ri          REAL    DEFAULT {INITIAL_ELO},
            goals_scored    INTEGER DEFAULT 0,
            goals_conceded  INTEGER DEFAULT 0,
            assists         INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            losses          INTEGER DEFAULT 0,
            draws           INTEGER DEFAULT 0,
            clean_sheets    INTEGER DEFAULT 0,
            win_streak      INTEGER DEFAULT 0,
            best_streak     INTEGER DEFAULT 0,
            banned_until    DATETIME,
            banned_reason   TEXT,
            last_elo_adjust TEXT,
            registered_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tournaments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            tournament_type TEXT NOT NULL DEFAULT 'vsa',
            stage           TEXT DEFAULT 'groups',
            groups_count    INTEGER DEFAULT 2,
            playoff_started INTEGER DEFAULT 0,
            created_by      INTEGER,
            description     TEXT,
            required_channel TEXT,
            is_official     INTEGER NOT NULL DEFAULT 1,
            chat_id         TEXT,
            playoff_slots   INTEGER NOT NULL DEFAULT 2,
            row_bg_alpha    INTEGER NOT NULL DEFAULT 255,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tournament_players (
            tournament_id   INTEGER,
            player_id       INTEGER,
            group_name      TEXT,
            group_points    INTEGER DEFAULT 0,
            group_gf        INTEGER DEFAULT 0,
            group_ga        INTEGER DEFAULT 0,
            group_wins      INTEGER DEFAULT 0,
            group_draws     INTEGER DEFAULT 0,
            group_losses    INTEGER DEFAULT 0,
            eliminated      INTEGER DEFAULT 0,
            PRIMARY KEY (tournament_id, player_id),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id   INTEGER,
            player1_id      INTEGER,
            player2_id      INTEGER,
            score1          INTEGER,
            score2          INTEGER,
            goals1_detail   TEXT,
            goals2_detail   TEXT,
            stats_extra     TEXT,
            stage           TEXT DEFAULT 'group',
            round_num       INTEGER DEFAULT 1,
            status          TEXT DEFAULT 'pending',
            reported_by     INTEGER,
            deadline        DATETIME,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            played_at       DATETIME,
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS playoff_brackets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id   INTEGER,
            stage           TEXT,
            match_id        INTEGER,
            position        INTEGER
        )
        """
    )
    # Per-tournament isolated ELO leaderboard. Used ONLY by tournaments where
    # tournaments.is_official = 0 (player-created). The global pools
    # (players.elo / elo_vsa / elo_ri) are never touched for these.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tournament_elo (
            tournament_id   INTEGER NOT NULL,
            player_id       INTEGER NOT NULL,
            elo             REAL    NOT NULL DEFAULT 0,
            games           INTEGER NOT NULL DEFAULT 0,
            wins            INTEGER NOT NULL DEFAULT 0,
            draws           INTEGER NOT NULL DEFAULT 0,
            losses          INTEGER NOT NULL DEFAULT 0,
            goals_for       INTEGER NOT NULL DEFAULT 0,
            goals_against   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tournament_id, player_id)
        )
        """
    )

    # Runtime-promoted bot admins. The env-var ADMIN_IDS list still acts as
    # the "root" admin set (cannot be revoked through the bot). Anyone added
    # to this table is allowed to use bot-wide admin commands (/ban, /elo,
    # /grant_admin, ...). This table no longer grants automatic write-access
    # to every tournament — for that, see ``tournament_admins`` below.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_admins (
            telegram_id  INTEGER PRIMARY KEY,
            granted_by   INTEGER,
            granted_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            note         TEXT
        )
        """
    )

    # Bot owners (super-admins). Owners sit above regular bot_admins in the
    # privilege hierarchy: they can do everything a bot admin can, plus
    # assign/revoke other owners. Root admins from ADMIN_IDS are implicitly
    # owners and do not need an entry here.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_owners (
            telegram_id  INTEGER PRIMARY KEY,
            granted_by   INTEGER,
            granted_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            note         TEXT
        )
        """
    )

    # Per-tournament admin delegation. Lets the tournament creator (or a
    # root admin from ADMIN_IDS) appoint additional managers for a single
    # tournament without giving them bot-wide admin powers. Membership
    # here grants exactly the same write rights as the creator: editing
    # description / channel binding, advancing stages, replacing players,
    # confirming matches in that tournament. It does NOT grant access to
    # other tournaments or to bot-wide commands.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tournament_admins (
            tournament_id   INTEGER NOT NULL,
            telegram_id     INTEGER NOT NULL,
            granted_by      INTEGER,
            granted_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            note            TEXT,
            PRIMARY KEY (tournament_id, telegram_id),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_tadmin_user "
        "ON tournament_admins(telegram_id)"
    )

    # Per-tournament audit log. Records every meaningful admin action
    # (player added/removed, match score changed, walkover applied,
    # stage advanced, description / channel changed, t-admins
    # assigned/removed). Used by /tlog to resolve disputes —
    # especially relevant now that several admins can co-manage one
    # tournament.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tournament_audit_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id   INTEGER NOT NULL,
            ts              DATETIME DEFAULT CURRENT_TIMESTAMP,
            actor_telegram_id INTEGER,
            actor_username  TEXT,
            action          TEXT NOT NULL,
            details         TEXT,
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_tlog_tid_ts "
        "ON tournament_audit_log(tournament_id, ts DESC)"
    )

    # ── Lightweight migrations for older DBs ─────────────────────────────────
    if not _column_exists(conn, "players", "game_nickname"):
        c.execute("ALTER TABLE players ADD COLUMN game_nickname TEXT")
    if not _column_exists(conn, "players", "banned_until"):
        c.execute("ALTER TABLE players ADD COLUMN banned_until DATETIME")
    if not _column_exists(conn, "players", "banned_reason"):
        c.execute("ALTER TABLE players ADD COLUMN banned_reason TEXT")
    if not _column_exists(conn, "players", "last_elo_adjust"):
        c.execute("ALTER TABLE players ADD COLUMN last_elo_adjust TEXT")
    if not _column_exists(conn, "players", "elo_vsa"):
        c.execute(f"ALTER TABLE players ADD COLUMN elo_vsa REAL DEFAULT {INITIAL_ELO}")
    if not _column_exists(conn, "players", "elo_ri"):
        c.execute(f"ALTER TABLE players ADD COLUMN elo_ri REAL DEFAULT {INITIAL_ELO}")
    if not _column_exists(conn, "players", "no_keyboard"):
        # Per-user preference: when 1, the bot suppresses the bottom reply
        # keyboard in DMs. Set/unset via /hide_keyboard / /show_keyboard.
        c.execute("ALTER TABLE players ADD COLUMN no_keyboard INTEGER DEFAULT 0")
    if not _column_exists(conn, "tournaments", "tournament_type"):
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN tournament_type TEXT NOT NULL DEFAULT 'vsa'"
        )
    if not _column_exists(conn, "tournaments", "created_by"):
        c.execute("ALTER TABLE tournaments ADD COLUMN created_by INTEGER")
    if not _column_exists(conn, "tournaments", "description"):
        c.execute("ALTER TABLE tournaments ADD COLUMN description TEXT")
    if not _column_exists(conn, "tournaments", "required_channel"):
        c.execute("ALTER TABLE tournaments ADD COLUMN required_channel TEXT")
    if not _column_exists(conn, "tournaments", "is_official"):
        # Default 1 keeps every existing tournament behaviorally identical
        # to before this migration: their matches continue to feed the global
        # ELO/ELO_VSA/ELO_RI pools. New player-created tournaments are
        # inserted with is_official=0 from the bot layer.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN is_official INTEGER NOT NULL DEFAULT 1"
        )
    if not _column_exists(conn, "tournaments", "chat_id"):
        # NULL = not bound to any chat. When set (Telegram chat_id as TEXT),
        # screenshots posted in that chat are auto-routed to this tournament.
        c.execute("ALTER TABLE tournaments ADD COLUMN chat_id TEXT")
    if not _column_exists(conn, "tournaments", "playoff_slots"):
        # How many players from each group advance to the playoff.
        # 2 matches the historical hard-coded default.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN playoff_slots INTEGER NOT NULL DEFAULT 2"
        )
    if not _column_exists(conn, "tournaments", "series_length"):
        # Best-of-N series length. 0/1 = single match (default behaviour).
        # 3 = best of 3 (first to 2 wins). 5 = best of 5. 7 = best of 7.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN series_length INTEGER NOT NULL DEFAULT 0"
        )
    if not _column_exists(conn, "tournaments", "auto_confirm"):
        # When 1: photo-OCR matches go straight to `confirmed` without the
        # opponent-button confirmation step (mimics WEEKEND CUP H2H behaviour).
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN auto_confirm INTEGER NOT NULL DEFAULT 0"
        )
    if not _column_exists(conn, "tournaments", "group_matches_per_pair"):
        # How many matches each pair plays inside a group.
        # 1 = single round-robin (current default).
        # 2 = double round-robin (home + away, like Champions League groups).
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN group_matches_per_pair INTEGER NOT NULL DEFAULT 1"
        )
    if not _column_exists(conn, "tournaments", "playoff_matches_per_pair"):
        # How many legs each playoff tie is played over.
        # 1 = single match.
        # 2 = two legs aggregated by goals; on aggregate tie, an extra
        # match is appended.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN playoff_matches_per_pair INTEGER NOT NULL DEFAULT 1"
        )
    if not _column_exists(conn, "tournaments", "reminder_dm_hours"):
        # How often to DM each player about their pending matches in this
        # tournament. 0 = disabled, default 12.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN reminder_dm_hours INTEGER NOT NULL DEFAULT 12"
        )
    if not _column_exists(conn, "tournaments", "reminder_chat_enabled"):
        # When 1, the bot posts periodic reminders in the bound chat with
        # an escalating cadence (every 6h → 3h → 30min as deadline approaches).
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN reminder_chat_enabled INTEGER NOT NULL DEFAULT 0"
        )
    if not _column_exists(conn, "tournaments", "deadline_at"):
        # Optional global deadline for the tournament — used by the chat
        # reminder cadence ("dd day"). Stored as ISO datetime string.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN deadline_at DATETIME"
        )
    if not _column_exists(conn, "tournaments", "target_group_size"):
        # Optional preferred number of players per group, set at creation
        # time. ``draw_groups`` uses this to compute groups_count when
        # ``groups_count`` itself isn't pre-set.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN target_group_size INTEGER"
        )
    if not _column_exists(conn, "tournaments", "bg_image_path"):
        # Per-tournament background image used by the rendered standings
        # and playoff bracket PNGs. Path on disk (relative to the bot's
        # working dir) or absolute. NULL = use the default flat-colour
        # background.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN bg_image_path TEXT"
        )
    if not _column_exists(conn, "tournaments", "bg_image_data"):
        # Base64-encoded JPEG/PNG bytes of the background image. Stored
        # in the DB so the image survives container redeploys (Railway,
        # Heroku, Docker rebuild) where the on-disk file is wiped. The
        # ``bg_image_path`` column is kept for local-disk caching only.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN bg_image_data TEXT"
        )
    if not _column_exists(conn, "tournaments", "playoff_advance_mode"):
        # 'wins' (default) — the player with more wins in the series
        # advances. 'goals' — the player with more total goals across
        # all matches in the pair advances (aggregate scoring).
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN playoff_advance_mode TEXT NOT NULL DEFAULT 'wins'"
        )
    if not _column_exists(conn, "tournaments", "playoff_stage_config"):
        # Per-stage overrides for the playoff series. JSON blob keyed by
        # stage code (``r16``, ``qf``, ``sf``, ``final`` …). Each value is
        # ``{"len": N, "mode": "wins"|"goals"}`` where ``len`` is the max
        # number of legs (1/3/5/7…) and ``mode`` is "wins" (first to
        # majority, early-stop allowed) or "goals" (play all N, aggregate
        # decides). Stages not present in the JSON fall back to the
        # tournament-wide ``playoff_matches_per_pair`` /
        # ``playoff_advance_mode``.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN playoff_stage_config TEXT NOT NULL DEFAULT '{}'"
        )
    if not _column_exists(conn, "tournaments", "row_bg_alpha"):
        # Row/card background opacity for rendered standings images.
        # 0 = fully transparent, 255 = fully opaque. Set via
        # /set_row_alpha <ID> <0-100> which converts percent → 0-255.
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN row_bg_alpha INTEGER NOT NULL DEFAULT 255"
        )
    if not _column_exists(conn, "tournaments", "ocr_mode"):
        # OCR recognition mode for match screenshots:
        # 'ai'         — full AI OCR: score + opponent nicknames (default)
        # 'score_only' — AI extracts only the score; user must specify
        #                the opponent via caption (@username) or /report
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN ocr_mode TEXT NOT NULL DEFAULT 'ai'"
        )
    # Per-player last-DM timestamp used by reminder loop to throttle.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_log (
            tournament_id   INTEGER NOT NULL,
            kind            TEXT NOT NULL,  -- 'dm:<player_id>' or 'chat'
            last_sent_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tournament_id, kind)
        )
        """
    )
    if not _column_exists(conn, "matches", "stats_extra"):
        c.execute("ALTER TABLE matches ADD COLUMN stats_extra TEXT")
    if not _column_exists(conn, "matches", "leg"):
        # 1-based ordinal of this match within its pair-tie.
        # 1 = first leg, 2 = second leg, 3 = extra match (aggregate-tie tiebreaker).
        c.execute("ALTER TABLE matches ADD COLUMN leg INTEGER NOT NULL DEFAULT 1")
    if not _column_exists(conn, "matches", "screenshot_hash"):
        # SHA256 of the screenshot used to confirm the result. Lets us reject
        # duplicate uploads ("Результат уже записан ранее") even when the user
        # re-sends the same album.
        c.execute("ALTER TABLE matches ADD COLUMN screenshot_hash TEXT")
    if not _column_exists(conn, "matches", "screenshot_file_id"):
        # Telegram file_id of the screenshot used to report the result.
        # Lets the bot forward the picture to the admin DM together with
        # the approve/reject buttons so admins can verify the score
        # against the actual screenshot.
        c.execute("ALTER TABLE matches ADD COLUMN screenshot_file_id TEXT")

    # Per-match goal events extracted from the screenshot (one row per goal).
    # ``player_id`` is the resolved league player; ``raw_name`` is whatever
    # OCR returned (kept for audit / re-resolve later). ``side`` is "home"
    # or "away" if we could detect it from the team-strip colour.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS match_goals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id        INTEGER NOT NULL,
            tournament_id   INTEGER,
            player_id       INTEGER,
            raw_name        TEXT,
            minute          INTEGER,
            side            TEXT,            -- 'home' | 'away' | NULL
            ord             INTEGER NOT NULL DEFAULT 0,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
            FOREIGN KEY (player_id) REFERENCES players(id)
        )
        """
    )
    # Lightweight migrations for older `match_goals` tables that pre-date
    # the per-side scorer ranking. CREATE TABLE IF NOT EXISTS above is a
    # no-op if the table already exists, so we have to ALTER manually
    # BEFORE we try to CREATE INDEX on the new columns. Without these
    # adds, /tablebomb crashes on Postgres with
    #     UndefinedColumn: column mg.side does not exist
    # because the older schema only had {match_id, player_id, raw_name,
    # minute, ord, created_at}.
    if not _column_exists(conn, "match_goals", "side"):
        c.execute("ALTER TABLE match_goals ADD COLUMN side TEXT")
    if not _column_exists(conn, "match_goals", "tournament_id"):
        c.execute("ALTER TABLE match_goals ADD COLUMN tournament_id INTEGER")
        # Backfill tournament_id from the parent matches row so /tablebomb
        # immediately works against historical data.
        try:
            c.execute(
                """UPDATE match_goals
                      SET tournament_id = (
                          SELECT m.tournament_id FROM matches m
                           WHERE m.id = match_goals.match_id
                      )
                    WHERE tournament_id IS NULL"""
            )
        except Exception:
            # Best-effort backfill; on broken FKs we just leave NULL and
            # the next /admin_addgoal / OCR insert will populate it.
            pass

    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_match_goals_match ON match_goals(match_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_match_goals_tournament ON match_goals(tournament_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_match_goals_player ON match_goals(player_id)"
    )

    # Dedicated table that records every screenshot we have already
    # processed. Independent from `matches` so we can also detect
    # double-submissions across different chats / sessions.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_screenshots (
            sha256          TEXT NOT NULL,
            tournament_id   INTEGER,
            chat_id         TEXT,
            match_id        INTEGER,
            reporter_id     INTEGER,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (sha256, tournament_id)
        )
        """
    )

    # ── Add tournament_id → tournaments(id) FK with ON DELETE CASCADE for ─
    # existing Postgres installs that pre-date the constraint. SQLite does
    # not support adding FK constraints to an existing table without
    # rebuilding it; new SQLite installs get the constraint via the CREATE
    # TABLE above, and rebuilding live SQLite tables here would risk data
    # loss for very little upside (the bot is deployed on Postgres).
    _maybe_add_tournament_fk(conn)

    # Auto-tech-loss: per-tournament configuration (added 2026-05).
    if not _column_exists(conn, "tournaments", "auto_tech_loss_enabled"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN auto_tech_loss_enabled INTEGER NOT NULL DEFAULT 0"
        )
    if not _column_exists(conn, "tournaments", "auto_tech_loss_score"):
        # Stored as 'X:Y' (string) so we don't have to define two columns.
        # NULL/empty → fall back to the bot-wide default ('0:3').
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN auto_tech_loss_score TEXT"
        )

    # Bracket-only tournament (no group stage; players go straight to a
    # seeded knockout bracket). Added 2026-05.
    if not _column_exists(conn, "tournaments", "bracket_only"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN bracket_only INTEGER NOT NULL DEFAULT 0"
        )

    # Groups-only tournament (no playoff; the top of the group table at
    # the end of the group stage is declared the winner). Added 2026-05.
    if not _column_exists(conn, "tournaments", "groups_only"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN groups_only INTEGER NOT NULL DEFAULT 0"
        )

    # Self-signup toggle (any registered player can join via the
    # "Tournaments" inline button without an admin's /add_player). Added
    # 2026-05. Defaults to 1 (open); admins can lock it via
    # /tournament_signup close. While open, players who tap "🙋
    # Записаться" land in the lobby group "?".
    if not _column_exists(conn, "tournaments", "open_signup"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN open_signup INTEGER NOT NULL DEFAULT 1"
        )

    # Bracket image layout. 'mirrored' = classic diamond bracket (default
    # for small brackets, ≤16 pairs at any stage), 'linear' = single
    # left-to-right column flow. Admins can toggle per tournament via
    # /set_bracket_layout. Added 2026-05.
    if not _column_exists(conn, "tournaments", "bracket_layout"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN bracket_layout TEXT DEFAULT 'mirrored'"
        )

    # 3rd-place match ("бронзовый финал") toggle. When enabled
    # (default), once the semifinals are over the bot spawns an extra
    # fixture between the two SF losers in parallel with the final.
    # Tournament only flips to ``stage='finished'`` after both the
    # final and the bronze match are confirmed. Disable by setting
    # to 0 (`/set_third_place <id> off` or via the settings panel).
    # Added 2026-05.
    if not _column_exists(conn, "tournaments", "playoff_third_place"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN playoff_third_place INTEGER NOT NULL DEFAULT 1"
        )

    # Penalty-shootout toggle. When enabled, the OCR layer is told to
    # extract the penalty scores that appear in parentheses next to
    # the regular score on FC Mobile end-screens (e.g. "(3) 3 - 3 (1)"
    # — 3:3 in regulation+ET, home wins on penalties 3:1). The
    # extracted ``pen1``/``pen2`` are stored on the match row and
    # used by ``_resolve_pair_winner`` as a final tiebreaker when the
    # aggregate is level. Group-stage matches are NOT affected — a
    # 3:3 in groups stays a draw regardless of the shootout. Default
    # 0 (off) so behaviour is unchanged for existing tournaments.
    # Added 2026-05.
    if not _column_exists(conn, "tournaments", "playoff_penalties"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN playoff_penalties INTEGER NOT NULL DEFAULT 0"
        )

    # Per-match penalty-shootout score. NULL = no shootout (regular
    # match decided in regulation/ET). Both columns are NULL together;
    # they are only populated for playoff matches in tournaments with
    # ``playoff_penalties=1`` when the OCR detects parenthesised
    # numbers next to the score.
    if not _column_exists(conn, "matches", "pen1"):
        c.execute("ALTER TABLE matches ADD COLUMN pen1 INTEGER")
    if not _column_exists(conn, "matches", "pen2"):
        c.execute("ALTER TABLE matches ADD COLUMN pen2 INTEGER")

    # Background overlay transparency (0–255). 0 = fully transparent overlay
    # (background fully visible), 255 = fully opaque overlay (background
    # invisible). Default 165 matches the previous hardcoded value.
    # Configurable via /set_overlay <ID> <0-100> (percentage).
    # Added 2026-05.
    if not _column_exists(conn, "tournaments", "bg_overlay_alpha"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN bg_overlay_alpha INTEGER NOT NULL DEFAULT 165"
        )

    # Draw mode for cup/playoff bracket: "auto" (seeded by ELO),
    # "random" (random shuffle), "manual" (admin picks pairs).
    # Added 2026-05.
    if not _column_exists(conn, "tournaments", "draw_mode"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN draw_mode TEXT NOT NULL DEFAULT 'auto'"
        )

    # Template ID that was used to create this tournament (NULL if none).
    if not _column_exists(conn, "tournaments", "template_id"):
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN template_id INTEGER"
        )

    # JSON config that, when present, marks this league as a template
    # that should automatically offer follow-up cups via the inline
    # "🏆 Создать кубки" button in the settings panel and a one-time
    # chat broadcast when the league reaches ``stage='groups_done'``.
    # Format: ``{"main_size": 24, "consolation_size": 8, "legs_per_pair": 2}``.
    # Empty/NULL = no follow-up suggestion. Set by the
    # ``champions_league_32`` template.
    if not _column_exists(conn, "tournaments", "followup_cups_config"):
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN followup_cups_config TEXT"
        )

    # IDs of the cups spawned from this league (set by spawn_cl_followup_cups
    # via /cl_spawn_cups or the inline button) so we don't accidentally
    # spawn twice. Stored as ``"<main_tid>:<cons_tid>"``; NULL = not spawned yet.
    if not _column_exists(conn, "tournaments", "followup_cups_tids"):
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN followup_cups_tids TEXT"
        )

    # ── Custom tournament templates table ────────────────────────────────
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tournament_templates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            created_by      INTEGER NOT NULL,
            config_json     TEXT NOT NULL,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Custom footer text appended to bot messages (match results, etc.).
    # Set by admin via the settings panel or /set_footer command.
    # NULL / empty = no footer. HTML allowed.
    if not _column_exists(conn, "tournaments", "footer_text"):
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN footer_text TEXT"
        )

    # Footer display scope (legacy, kept for migration compat):
    if not _column_exists(conn, "tournaments", "footer_scope"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN footer_scope TEXT NOT NULL DEFAULT 'all'"
        )

    # Per-message-type footer toggles. JSON object with boolean fields:
    #   match, table, playoff, stage, reminder, broadcast, finish
    # All default to true (show everywhere). Admins toggle individual
    # types on/off via the settings panel.
    if not _column_exists(conn, "tournaments", "footer_places"):
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN footer_places TEXT"
        )

    # ── Signup-phase reminders (2026-05) ─────────────────────────────────
    # When ``signup_reminder_minutes`` > 0 AND ``open_signup`` = 1 AND
    # the tournament is bound to a chat AND no matches have been
    # generated yet, the reminder loop posts a periodic chat message
    # nagging unregistered players to sign up. The interval is taken
    # verbatim from ``signup_reminder_minutes`` (admin sets it via
    # ``/set_signup_reminder``). 0 = disabled (default).
    if not _column_exists(conn, "tournaments", "signup_reminder_minutes"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN signup_reminder_minutes INTEGER NOT NULL DEFAULT 0"
        )
    # Optional admin-set deadline for the registration window. Stored
    # as ISO datetime string ('YYYY-MM-DD HH:MM:SS', UTC). Shown in the
    # reminder message; does NOT auto-close signup on its own — admins
    # still flip ``open_signup`` themselves (or it auto-closes when the
    # group draw runs).
    if not _column_exists(conn, "tournaments", "signup_deadline_at"):
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN signup_deadline_at DATETIME"
        )
    # Optional admin-supplied registration link / form URL / instructions
    # text. Appended verbatim to every signup reminder message. NULL or
    # empty → reminder just shows the inline "🙋 Записаться" button.
    if not _column_exists(conn, "tournaments", "signup_link"):
        c.execute(
            "ALTER TABLE tournaments ADD COLUMN signup_link TEXT"
        )

    # ── Per-tournament team / club tag for participants (2026-05) ────────
    # Free-form short label (≤32 chars) shown next to the player's name
    # in standings PNG, playoff bracket PNG, podium message, summary
    # report, reminders, etc. Per-tournament so the same Telegram user
    # can play for "Реал" in one tournament and "Спартак" in another.
    # NULL / empty = no tag.
    if not _column_exists(conn, "tournament_players", "team_tag"):
        c.execute(
            "ALTER TABLE tournament_players ADD COLUMN team_tag TEXT"
        )

    # ── Player titles / awards (2026-05) ─────────────────────────────────
    # Free-form titles awarded to players by admins. Multiple titles per
    # player allowed (granted_at orders the list). Shown in /profile and
    # in text-table / tablebomb listings as a small badge after the name.
    # ``title`` is admin-typed text including any emojis they want
    # (e.g. "🐐 GOAT" or "Чемпион №1"); ``note`` is an optional reason.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS player_titles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id     INTEGER NOT NULL,
            title         TEXT NOT NULL,
            granted_by    INTEGER,
            granted_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            note          TEXT,
            FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_player_titles_player "
        "ON player_titles(player_id)"
    )

    # ── Quotes & per-chat quote settings (2026-05) ───────────────────────
    # User-submitted quotations the bot rotates through every N minutes
    # in chats that opt in via /set_quote_interval. ``author`` is the
    # *attribution* (free-form text — "Pep", "@somebody", "Народная
    # мудрость"); ``added_by`` is the player_id of the registered user
    # who submitted the quote (for /delquote / audit).
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quotes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     TEXT,
            text        TEXT NOT NULL,
            author      TEXT,
            added_by    INTEGER,
            added_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_chat ON quotes(chat_id)"
    )

    # Per-chat settings (interval in minutes for the quote loop, 0 =
    # disabled; ``last_quote_at`` is updated by the job to throttle).
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id                  TEXT PRIMARY KEY,
            quote_interval_minutes   INTEGER NOT NULL DEFAULT 0,
            last_quote_at            DATETIME
        )
        """
    )
    # Quiet hours for the quote-rotation loop. Stored as the start /
    # end hour in the operator's display TZ (МСК by default).
    # Defaults: 23..12 — i.e. quotes are posted only between 12:00
    # and 23:00 МСК; from 23:00 to 12:00 the loop stays silent so the
    # bot doesn't ping at night.
    if not _column_exists(conn, "chat_settings", "quiet_start_hour"):
        c.execute(
            "ALTER TABLE chat_settings "
            "ADD COLUMN quiet_start_hour INTEGER NOT NULL DEFAULT 23"
        )
    if not _column_exists(conn, "chat_settings", "quiet_end_hour"):
        c.execute(
            "ALTER TABLE chat_settings "
            "ADD COLUMN quiet_end_hour INTEGER NOT NULL DEFAULT 12"
        )

    # Voice message support for quotes — nullable file_id so we can
    # repost voice messages as quotes too.
    if not _column_exists(conn, "quotes", "voice_file_id"):
        c.execute(
            "ALTER TABLE quotes ADD COLUMN voice_file_id TEXT"
        )

    # Playoff pairing mode for 4-group tournaments.
    # 'auto' (default): interleave by group strength.
    # 'pairs': pair groups as (A,C) and (B,D) — A1-C2, B2-D1, A2-C1, B1-D2.
    # Added 2026-06.
    if not _column_exists(conn, "tournaments", "playoff_pairing"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN playoff_pairing TEXT NOT NULL DEFAULT 'auto'"
        )

    # Custom display name for the single group in a league (Лига, Сетка 1, etc.)
    # NULL = fall back to "Группа A".
    if not _column_exists(conn, "tournaments", "group_display_name"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN group_display_name TEXT"
        )

    # ── Tours (rounds) support ────────────────────────────────────────
    if not _column_exists(conn, "tournaments", "tours_enabled"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN tours_enabled INTEGER DEFAULT 0"
        )
    if not _column_exists(conn, "tournaments", "total_tours"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN total_tours INTEGER DEFAULT 0"
        )
    if not _column_exists(conn, "tournaments", "current_tour"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN current_tour INTEGER DEFAULT 0"
        )
    if not _column_exists(conn, "tournaments", "auto_next_tour"):
        c.execute(
            "ALTER TABLE tournaments "
            "ADD COLUMN auto_next_tour INTEGER DEFAULT 0"
        )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tournament_tours (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id   INTEGER NOT NULL,
            tour_number     INTEGER NOT NULL,
            status          TEXT DEFAULT 'active',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tournament_id, tour_number)
        )
        """
    )

    if not _column_exists(conn, "matches", "tour_number"):
        c.execute(
            "ALTER TABLE matches "
            "ADD COLUMN tour_number INTEGER DEFAULT 0"
        )

    conn.commit()
    conn.close()


def _maybe_add_tournament_fk(conn) -> None:
    """Add ON DELETE CASCADE FK from {tournament_players, matches}.tournament_id
    → tournaments(id), but only on Postgres and only if not already present.

    Uses ``NOT VALID`` to avoid scanning existing rows; new inserts/updates
    are fully validated. The constraint is then validated lazily — orphan
    rows in legacy data won't block the migration.
    """
    if conn.backend != "postgres":
        return
    for table, fk_name in (
        ("tournament_players", "fk_tp_tournament"),
        ("matches",            "fk_match_tournament"),
    ):
        try:
            row = conn.execute(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE table_name = ? AND constraint_name = ? LIMIT 1",
                (table, fk_name),
            ).fetchone()
        except Exception:
            continue
        if row:
            continue
        try:
            conn.execute(
                f"ALTER TABLE {table} "
                f"ADD CONSTRAINT {fk_name} "
                f"FOREIGN KEY (tournament_id) "
                f"REFERENCES tournaments(id) ON DELETE CASCADE NOT VALID"
            )
        except Exception:
            # Migration is best-effort: an admin can fix orphaned rows
            # manually and re-run init_db. Don't crash the bot on startup.
            pass


# ── Player helpers ────────────────────────────────────────────────────────────

def upsert_player(username: str, telegram_id: int | None = None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        f"""INSERT INTO players (username, telegram_id, elo)
           VALUES (?, ?, {INITIAL_ELO})
           ON CONFLICT(username) DO UPDATE SET
               telegram_id = COALESCE(excluded.telegram_id, players.telegram_id)""",
        (username.lower(), telegram_id),
    )
    conn.commit()
    player = c.execute(
        "SELECT * FROM players WHERE username=?", (username.lower(),)
    ).fetchone()
    conn.close()
    return dict(player)


def get_player(username: str):
    conn = get_conn()
    p = conn.execute(
        "SELECT * FROM players WHERE username=?", (username.lower(),)
    ).fetchone()
    conn.close()
    return dict(p) if p else None


def get_player_by_id(pid: int):
    conn = get_conn()
    p = conn.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(p) if p else None


def update_player_username(player_id: int, new_username: str) -> None:
    """Rewrite a player's @username (used when their Telegram handle changed)."""
    conn = get_conn()
    conn.execute(
        "UPDATE players SET username=? WHERE id=?",
        (new_username.lower(), int(player_id)),
    )
    conn.commit()
    conn.close()


def merge_players(keep_id: int, drop_id: int) -> dict:
    """Merge ``drop_id`` into ``keep_id`` and delete the drop row.

    Reassigns every reference (tournament_players, matches at any
    status, match_goals, tournament_elo) from ``drop_id`` to
    ``keep_id``. When both ids share a row in ``tournament_players``
    or ``tournament_elo``, per-row counters are summed onto the kept
    side before the duplicate is removed.

    Returns ``{"matches_moved": int, "tp_overlap": int, "elo_overlap":
    int, "goals_moved": int}`` so the caller can surface concrete
    numbers to the admin.
    """
    if int(keep_id) == int(drop_id):
        raise ValueError("keep_id and drop_id must differ")
    keep = get_player_by_id(keep_id)
    drop = get_player_by_id(drop_id)
    if not keep:
        raise LookupError(f"keep player id={keep_id} not found")
    if not drop:
        raise LookupError(f"drop player id={drop_id} not found")

    conn = get_conn()
    c = conn.cursor()
    try:
        # 1. tournament_players: sum stats on overlap, reassign on disjoint.
        drop_tps = [
            dict(r) for r in c.execute(
                "SELECT * FROM tournament_players WHERE player_id=?",
                (drop_id,),
            ).fetchall()
        ]
        keep_tids = {
            r["tournament_id"] if isinstance(r, dict) else r[0]
            for r in c.execute(
                "SELECT tournament_id FROM tournament_players WHERE player_id=?",
                (keep_id,),
            ).fetchall()
        }
        tp_overlap = 0
        for row in drop_tps:
            tid = row["tournament_id"]
            if tid in keep_tids:
                # Overlap: add drop's group counters onto keep's row,
                # then remove the duplicate.
                c.execute(
                    """UPDATE tournament_players SET
                            group_points = COALESCE(group_points,0) + ?,
                            group_gf     = COALESCE(group_gf,0)     + ?,
                            group_ga     = COALESCE(group_ga,0)     + ?,
                            group_wins   = COALESCE(group_wins,0)   + ?,
                            group_draws  = COALESCE(group_draws,0)  + ?,
                            group_losses = COALESCE(group_losses,0) + ?
                       WHERE tournament_id=? AND player_id=?""",
                    (
                        row.get("group_points") or 0,
                        row.get("group_gf") or 0,
                        row.get("group_ga") or 0,
                        row.get("group_wins") or 0,
                        row.get("group_draws") or 0,
                        row.get("group_losses") or 0,
                        tid, keep_id,
                    ),
                )
                c.execute(
                    "DELETE FROM tournament_players "
                    "WHERE player_id=? AND tournament_id=?",
                    (drop_id, tid),
                )
                tp_overlap += 1
            else:
                c.execute(
                    "UPDATE tournament_players SET player_id=? "
                    "WHERE player_id=? AND tournament_id=?",
                    (keep_id, drop_id, tid),
                )

        # 2. matches: re-tag ALL matches (any status) so historical
        #    records don't end up orphaned when the drop row is
        #    deleted. Self-matches between drop and keep would be
        #    nonsensical post-merge, so wipe those.
        c.execute(
            "DELETE FROM matches WHERE "
            "(player1_id=? AND player2_id=?) OR (player1_id=? AND player2_id=?)",
            (keep_id, drop_id, drop_id, keep_id),
        )
        c.execute(
            "UPDATE matches SET player1_id=? WHERE player1_id=?",
            (keep_id, drop_id),
        )
        moved_p1 = c.rowcount or 0
        c.execute(
            "UPDATE matches SET player2_id=? WHERE player2_id=?",
            (keep_id, drop_id),
        )
        moved_p2 = c.rowcount or 0
        matches_moved = int(moved_p1) + int(moved_p2)

        # 3. match_goals: re-tag goal rows (best effort — older installs
        #    may not have the table). Wrapped in a SAVEPOINT so that if
        #    the table is missing on Postgres, we can rollback ONLY this
        #    sub-step instead of poisoning the whole transaction (which
        #    would fail every subsequent statement with
        #    ``current transaction is aborted, commands ignored until
        #    end of transaction block``).
        goals_moved = 0
        try:
            c.execute("SAVEPOINT sp_match_goals")
            try:
                c.execute(
                    "UPDATE match_goals SET player_id=? WHERE player_id=?",
                    (keep_id, drop_id),
                )
                goals_moved = int(c.rowcount or 0)
                c.execute("RELEASE SAVEPOINT sp_match_goals")
            except Exception:
                c.execute("ROLLBACK TO SAVEPOINT sp_match_goals")
                c.execute("RELEASE SAVEPOINT sp_match_goals")
                goals_moved = 0
        except Exception:
            # Backend doesn't support savepoints (very rare). Treat as
            # "no goals to move" and continue with the merge.
            goals_moved = 0

        # 4. tournament_elo: sum on overlap, reassign on disjoint. Same
        #    savepoint pattern — older DBs may lack the table or some
        #    of its columns.
        elo_overlap = 0
        try:
            c.execute("SAVEPOINT sp_tournament_elo")
            try:
                drop_elo = [
                    dict(r) for r in c.execute(
                        "SELECT * FROM tournament_elo WHERE player_id=?",
                        (drop_id,),
                    ).fetchall()
                ]
                keep_elo_tids = {
                    (r["tournament_id"] if isinstance(r, dict) else r[0])
                    for r in c.execute(
                        "SELECT tournament_id FROM tournament_elo WHERE player_id=?",
                        (keep_id,),
                    ).fetchall()
                }
                for row in drop_elo:
                    tid = row["tournament_id"]
                    if tid in keep_elo_tids:
                        # Overlap: pick the higher ELO, sum game counters.
                        c.execute(
                            """UPDATE tournament_elo SET
                                    elo           = MAX(elo, ?),
                                    games         = COALESCE(games,0)         + ?,
                                    wins          = COALESCE(wins,0)          + ?,
                                    draws         = COALESCE(draws,0)         + ?,
                                    losses        = COALESCE(losses,0)        + ?,
                                    goals_for     = COALESCE(goals_for,0)     + ?,
                                    goals_against = COALESCE(goals_against,0) + ?
                               WHERE tournament_id=? AND player_id=?""",
                            (
                                row.get("elo") or 0,
                                row.get("games") or 0,
                                row.get("wins") or 0,
                                row.get("draws") or 0,
                                row.get("losses") or 0,
                                row.get("goals_for") or 0,
                                row.get("goals_against") or 0,
                                tid, keep_id,
                            ),
                        )
                        c.execute(
                            "DELETE FROM tournament_elo "
                            "WHERE player_id=? AND tournament_id=?",
                            (drop_id, tid),
                        )
                        elo_overlap += 1
                    else:
                        c.execute(
                            "UPDATE tournament_elo SET player_id=? "
                            "WHERE player_id=? AND tournament_id=?",
                            (keep_id, drop_id, tid),
                        )
                c.execute("RELEASE SAVEPOINT sp_tournament_elo")
            except Exception:
                c.execute("ROLLBACK TO SAVEPOINT sp_tournament_elo")
                c.execute("RELEASE SAVEPOINT sp_tournament_elo")
                elo_overlap = 0
        except Exception:
            elo_overlap = 0

        # 5. Promote drop's stronger global counters onto the kept row.
        #    Schema drift on the ``players`` table (e.g. ``elo_vsa``
        #    column missing on a very old DB) used to abort the whole
        #    transaction with the cryptic ``current transaction is
        #    aborted`` message. SAVEPOINT keeps the merge atomic for
        #    the parts that DO exist.
        try:
            c.execute("SAVEPOINT sp_promote_player")
            try:
                c.execute(
                    """UPDATE players SET
                            elo            = MAX(elo,            ?),
                            elo_vsa        = MAX(elo_vsa,        ?),
                            elo_ri         = MAX(elo_ri,         ?),
                            goals_scored   = COALESCE(goals_scored,0)   + ?,
                            goals_conceded = COALESCE(goals_conceded,0) + ?,
                            assists        = COALESCE(assists,0)        + ?,
                            wins           = COALESCE(wins,0)           + ?,
                            losses         = COALESCE(losses,0)         + ?,
                            draws          = COALESCE(draws,0)          + ?,
                            clean_sheets   = COALESCE(clean_sheets,0)   + ?,
                            best_streak    = MAX(best_streak,    ?)
                       WHERE id=?""",
                    (
                        drop.get("elo") or 0,
                        drop.get("elo_vsa") or 0,
                        drop.get("elo_ri") or 0,
                        drop.get("goals_scored") or 0,
                        drop.get("goals_conceded") or 0,
                        drop.get("assists") or 0,
                        drop.get("wins") or 0,
                        drop.get("losses") or 0,
                        drop.get("draws") or 0,
                        drop.get("clean_sheets") or 0,
                        drop.get("best_streak") or 0,
                        keep_id,
                    ),
                )
                # Promote game_nickname only if keep doesn't have one.
                if not (keep.get("game_nickname") or "").strip() and (
                    drop.get("game_nickname") or ""
                ).strip():
                    c.execute(
                        "UPDATE players SET game_nickname=? WHERE id=?",
                        (drop.get("game_nickname"), keep_id),
                    )
                c.execute("RELEASE SAVEPOINT sp_promote_player")
            except Exception:
                c.execute("ROLLBACK TO SAVEPOINT sp_promote_player")
                c.execute("RELEASE SAVEPOINT sp_promote_player")
        except Exception:
            pass

        # 6. Drop the duplicate row.
        c.execute("DELETE FROM players WHERE id=?", (drop_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    return {
        "matches_moved": matches_moved,
        "tp_overlap": tp_overlap,
        "elo_overlap": elo_overlap,
        "goals_moved": goals_moved,
    }


def get_player_by_telegram_id(tid: int | None):
    """Return the player row matching ``telegram_id`` or None."""
    if tid is None:
        return None
    conn = get_conn()
    p = conn.execute(
        "SELECT * FROM players WHERE telegram_id=?", (int(tid),)
    ).fetchone()
    conn.close()
    return dict(p) if p else None


def get_player_by_game_nickname(nick: str | None):
    """Return the player row matching ``game_nickname`` or None."""
    if not nick:
        return None
    conn = get_conn()
    p = conn.execute(
        "SELECT * FROM players WHERE LOWER(game_nickname)=LOWER(?)",
        (nick.strip(),),
    ).fetchone()
    conn.close()
    return dict(p) if p else None


def find_players_by_fuzzy_game_nickname(query: str) -> list[tuple[dict, float]]:
    """Return ``[(player_dict, score), ...]`` whose ``game_nickname``
    contains ``query`` (case-insensitive). ``score`` is a 0..1
    similarity ranking — exact match → 1.0, otherwise a simple
    containment ratio.
    """
    if not query:
        return []
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM players
           WHERE LOWER(game_nickname) LIKE LOWER(?)
           ORDER BY elo DESC LIMIT 20""",
        (f"%{query.strip()}%",),
    ).fetchall()
    conn.close()
    q = query.strip().lower()
    out: list[tuple[dict, float]] = []
    for r in rows:
        p = dict(r)
        nick = (p.get("game_nickname") or "").lower()
        if nick == q:
            score = 1.0
        elif q in nick:
            score = len(q) / max(len(nick), 1)
        else:
            score = 0.0
        out.append((p, score))
    return out


def get_all_players_by_elo_field(field: str) -> list[dict]:
    """Return all players ordered by the given ELO field desc."""
    if field not in ("elo", "elo_vsa", "elo_ri"):
        field = "elo"
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM players WHERE {field} IS NOT NULL "
        f"ORDER BY {field} DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Tours (rounds) helpers ─────────────────────────────────────────────────────


def create_tournament_tour(tid: int, tour_number: int) -> int | None:
    """Insert a row into tournament_tours. Returns id or None on conflict."""
    conn = get_conn()
    try:
        row_id = conn.insert_returning_id(
            "INSERT INTO tournament_tours (tournament_id, tour_number) VALUES (?, ?)",
            (tid, tour_number),
        )
        conn.commit()
        return row_id
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def get_tournament_tours(tid: int) -> list[dict]:
    """All tour records for a tournament, ordered by tour_number."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tournament_tours WHERE tournament_id=? ORDER BY tour_number",
        (tid,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tour_matches(tid: int, tour_number: int) -> list[dict]:
    """All matches in a specific tour."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM matches WHERE tournament_id=? AND tour_number=?",
        (tid, tour_number),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_next_tour_number(tid: int) -> int:
    """Next available tour number (max existing + 1, or 1 if none)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(MAX(tour_number), 0) + 1 FROM tournament_tours WHERE tournament_id=?",
        (tid,),
    ).fetchone()
    conn.close()
    if row is None:
        return 1
    # Postgres returns RealDictCursor (dict-like), SQLite returns tuple.
    # Normalise to a plain int without depending on either shape.
    try:
        return int(row[0])
    except (KeyError, TypeError):
        try:
            return int(list(row.values())[0])
        except Exception:
            return 1


def set_current_tour(tid: int, n: int) -> None:
    """Update current_tour on the tournaments row."""
    conn = get_conn()
    conn.execute("UPDATE tournaments SET current_tour=? WHERE id=?", (n, tid))
    conn.commit()
    conn.close()


def set_tour_status(tid: int, tour_number: int, status: str) -> None:
    """Set status for a tour (active/completed)."""
    conn = get_conn()
    conn.execute(
        "UPDATE tournament_tours SET status=? WHERE tournament_id=? AND tour_number=?",
        (status, tid, tour_number),
    )
    conn.commit()
    conn.close()


def is_tour_complete(tid: int, tour_number: int) -> bool:
    """True when all matches in the tour have status='confirmed'."""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM matches "
        "WHERE tournament_id=? AND tour_number=? AND status != 'confirmed'",
        (tid, tour_number),
    ).fetchone()
    conn.close()
    if row is None:
        return True
    try:
        cnt = int(row[0])
    except (KeyError, TypeError):
        try:
            cnt = int(list(row.values())[0])
        except Exception:
            return True
    return cnt == 0



def get_all_players():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM players ORDER BY elo DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_player_stats(player_id, **kwargs):
    if not kwargs:
        return
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [player_id]
    conn.execute(f"UPDATE players SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


def set_game_nickname(player_id: int, game_nickname: str):
    conn = get_conn()
    conn.execute(
        "UPDATE players SET game_nickname=? WHERE id=?",
        (game_nickname, player_id),
    )
    conn.commit()
    conn.close()


def set_no_keyboard_preference(player_id: int, hide: bool):
    """When ``hide`` is True, the user has asked us to suppress the bottom
    reply keyboard in DMs."""
    conn = get_conn()
    conn.execute(
        "UPDATE players SET no_keyboard=? WHERE id=?",
        (1 if hide else 0, player_id),
    )
    conn.commit()
    conn.close()


def get_no_keyboard_preference(player_id: int) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT no_keyboard FROM players WHERE id=?", (player_id,)
    ).fetchone()
    conn.close()
    if not row:
        return False
    return bool(row["no_keyboard"])


# ── Match goals (top scorers via OCR) ─────────────────────────────────────────

def set_match_goals(match_id: int, goals: list[dict]) -> None:
    """
    Replace the goal-event list for ``match_id``.

    ``goals`` is a list of dicts with the schema:
        {"player_id": int|None, "raw_name": str, "minute": int|None,
         "side": "home"|"away"|None}
    The caller is responsible for resolving ``player_id`` (fuzzy-matching the
    raw OCR name to a registered player) — we only store whatever was given.
    """
    conn = get_conn()
    # Look up tournament_id from the match itself so the join in
    # /top_scorers stays cheap.
    row = conn.execute(
        "SELECT tournament_id FROM matches WHERE id=?", (match_id,)
    ).fetchone()
    tid = row["tournament_id"] if row else None

    conn.execute("DELETE FROM match_goals WHERE match_id=?", (match_id,))
    for i, g in enumerate(goals or []):
        conn.execute(
            """INSERT INTO match_goals
                   (match_id, tournament_id, player_id, raw_name, minute, side, ord)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (match_id, tid, g.get("player_id"),
             (g.get("raw_name") or "").strip() or None,
             g.get("minute"), g.get("side"), i),
        )
    conn.commit()
    conn.close()


def get_match_goals(match_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM match_goals
           WHERE match_id=? ORDER BY ord, id""",
        (match_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_top_scorers_global(limit: int = 20, only_official: bool = True) -> list[dict]:
    """
    Aggregate goals across all official tournaments. Excludes "unknown_scorer"
    rows (player_id IS NULL) so we don't over-count.
    """
    conn = get_conn()
    if only_official:
        rows = conn.execute(
            """SELECT mg.player_id AS player_id, COUNT(*) AS goals,
                      p.username   AS username
                 FROM match_goals mg
                 JOIN matches     m  ON m.id = mg.match_id
                 JOIN tournaments t  ON t.id = m.tournament_id
                 JOIN players     p  ON p.id = mg.player_id
                WHERE mg.player_id IS NOT NULL
                  AND COALESCE(t.is_official, 1) = 1
                  AND m.status = 'confirmed'
             GROUP BY mg.player_id, p.username
             ORDER BY goals DESC, p.username ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT mg.player_id AS player_id, COUNT(*) AS goals,
                      p.username   AS username
                 FROM match_goals mg
                 JOIN matches     m  ON m.id = mg.match_id
                 JOIN players     p  ON p.id = mg.player_id
                WHERE mg.player_id IS NOT NULL
                  AND m.status = 'confirmed'
             GROUP BY mg.player_id, p.username
             ORDER BY goals DESC, p.username ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_top_scorers_for_tournament(tournament_id: int, limit: int = 20) -> list[dict]:
    """Per-tournament top scorers (works for both official and custom)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT mg.player_id AS player_id, COUNT(*) AS goals,
                  p.username   AS username
             FROM match_goals mg
             JOIN matches m ON m.id = mg.match_id
             JOIN players p ON p.id = mg.player_id
            WHERE mg.tournament_id = ?
              AND mg.player_id IS NOT NULL
              AND m.status = 'confirmed'
         GROUP BY mg.player_id, p.username
         ORDER BY goals DESC, p.username ASC
            LIMIT ?""",
        (tournament_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_top_scorers_custom(limit: int = 20) -> list[dict]:
    """Aggregate across non-official ("custom" — created by regular players)
    tournaments. Useful for a separate leaderboard so admin-run leagues don't
    drown out small private cups."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT mg.player_id AS player_id, COUNT(*) AS goals,
                  p.username   AS username
             FROM match_goals mg
             JOIN matches     m ON m.id = mg.match_id
             JOIN tournaments t ON t.id = m.tournament_id
             JOIN players     p ON p.id = mg.player_id
            WHERE mg.player_id IS NOT NULL
              AND COALESCE(t.is_official, 1) = 0
              AND m.status = 'confirmed'
         GROUP BY mg.player_id, p.username
         ORDER BY goals DESC, p.username ASC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_top_scorers_by_side_for_tournament(
    tournament_id: int, limit: int = 50
) -> list[dict]:
    """Per-tournament scorers credited by side (home/away) of the match.

    For each goal in ``match_goals`` of ``tournament_id`` (only confirmed
    matches), credit the goal to the home or away participant of the
    parent ``matches`` row depending on ``mg.side``:
      * side='home'  → matches.player1_id
      * side='away'  → matches.player2_id

    Goals with NULL/unknown side are ignored (they shouldn't normally
    happen — OCR records 'home'/'away' for every recognised event).

    Returns rows ``{player_id, username, game_nickname, telegram_id,
    home_goals, away_goals, total_goals}`` sorted by total desc.
    """
    conn = get_conn()
    # NB on the GROUP BY: Postgres refuses to accept ``GROUP BY player_id``
    # here because ``player_id`` is also a real column on ``match_goals``
    # (``mg.player_id``), so the name resolves to the column instead of the
    # SELECT alias. After that, the CASE expression's ``mg.side`` /
    # ``m.player1_id`` / ``m.player2_id`` are no longer covered by either
    # the GROUP BY or an aggregate, so Postgres raises GroupingError. The
    # positional ``GROUP BY 1`` reliably groups by the SELECT expression on
    # both SQLite and Postgres without ambiguity.
    rows = conn.execute(
        """SELECT
               CASE WHEN mg.side='home' THEN m.player1_id
                    WHEN mg.side='away' THEN m.player2_id
                    ELSE NULL END                         AS scorer_id,
               SUM(CASE WHEN mg.side='home' THEN 1 ELSE 0 END) AS home_goals,
               SUM(CASE WHEN mg.side='away' THEN 1 ELSE 0 END) AS away_goals,
               COUNT(*)                                  AS total_goals
           FROM match_goals mg
           JOIN matches m ON m.id = mg.match_id
          WHERE mg.tournament_id = ?
            AND m.status = 'confirmed'
            AND mg.side IN ('home','away')
       GROUP BY 1
       ORDER BY total_goals DESC""",
        (tournament_id,),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        pid = r["scorer_id"]
        if pid is None:
            continue
        p = conn.execute(
            "SELECT username, game_nickname, telegram_id FROM players "
            "WHERE id=?",
            (pid,),
        ).fetchone()
        out.append({
            "player_id":     pid,
            "username":      (p["username"] if p else None),
            "game_nickname": (p["game_nickname"] if p else None),
            "telegram_id":   (p["telegram_id"] if p else None),
            "home_goals":    int(r["home_goals"] or 0),
            "away_goals":    int(r["away_goals"] or 0),
            "total_goals":   int(r["total_goals"] or 0),
        })
    conn.close()
    out = out[:limit]
    return out


def get_footballer_scorers_for_tournament(
    tournament_id: int, limit: int = 50
) -> list[dict]:
    """Per-tournament scorers grouped by in-game footballer name (raw_name).

    Returns rows ``{raw_name, scorer_id, username, game_nickname,
    home_goals, away_goals, total_goals}`` sorted by total desc.
    Each row represents a unique (raw_name, scorer_id) pair so that
    footballers used by different participants are listed separately.
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT
               mg.raw_name,
               CASE WHEN mg.side='home' THEN m.player1_id
                    WHEN mg.side='away' THEN m.player2_id
                    ELSE NULL END                         AS scorer_id,
               SUM(CASE WHEN mg.side='home' THEN 1 ELSE 0 END) AS home_goals,
               SUM(CASE WHEN mg.side='away' THEN 1 ELSE 0 END) AS away_goals,
               COUNT(*)                                  AS total_goals
           FROM match_goals mg
           JOIN matches m ON m.id = mg.match_id
          WHERE mg.tournament_id = ?
            AND m.status = 'confirmed'
            AND mg.side IN ('home','away')
            AND mg.raw_name IS NOT NULL
            AND mg.raw_name != ''
       GROUP BY mg.raw_name, 2
       ORDER BY total_goals DESC""",
        (tournament_id,),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        pid = r["scorer_id"]
        if pid is None:
            continue
        p = conn.execute(
            "SELECT username, game_nickname, telegram_id FROM players "
            "WHERE id=?",
            (pid,),
        ).fetchone()
        out.append({
            "raw_name":      r["raw_name"],
            "player_id":     pid,
            "username":      (p["username"] if p else None),
            "game_nickname": (p["game_nickname"] if p else None),
            "telegram_id":   (p["telegram_id"] if p else None),
            "home_goals":    int(r["home_goals"] or 0),
            "away_goals":    int(r["away_goals"] or 0),
            "total_goals":   int(r["total_goals"] or 0),
        })
    conn.close()
    out = out[:limit]
    return out


# ── Match goal CRUD (for /admin_addgoal & friends) ───────────────────────────

def get_match_goal(goal_id: int) -> dict | None:
    """Return a single ``match_goals`` row by id, or None."""
    conn = get_conn()
    r = conn.execute(
        "SELECT * FROM match_goals WHERE id=?", (goal_id,)
    ).fetchone()
    conn.close()
    return dict(r) if r else None


def add_match_goal(
    match_id: int,
    player_id: int | None,
    raw_name: str | None = None,
    minute: int | None = None,
    side: str | None = None,
) -> int:
    """Append a single goal event to ``match_id``. Returns the new goal id.

    Unlike ``set_match_goals`` (which replaces the full list), this is a
    targeted insert used by ``/admin_addgoal``. ``ord`` is auto-set to
    ``max(existing) + 1`` so new goals show up at the bottom of the list.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT tournament_id FROM matches WHERE id=?", (match_id,)
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"match {match_id} not found")
    tid = row["tournament_id"]

    last = conn.execute(
        "SELECT COALESCE(MAX(ord), -1) AS m FROM match_goals WHERE match_id=?",
        (match_id,),
    ).fetchone()
    next_ord = (last["m"] if last and last["m"] is not None else -1) + 1

    gid = conn.insert_returning_id(
        """INSERT INTO match_goals
               (match_id, tournament_id, player_id, raw_name, minute, side, ord)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            match_id, tid, player_id,
            (raw_name or "").strip() or None,
            minute, side, next_ord,
        ),
    )
    conn.commit()
    conn.close()
    return gid


def delete_match_goal(goal_id: int) -> bool:
    """Remove a single goal by id. Returns True if a row was deleted."""
    conn = get_conn()
    cur = conn.execute("DELETE FROM match_goals WHERE id=?", (goal_id,))
    deleted = bool(getattr(cur, "rowcount", 0) or 0)
    conn.commit()
    conn.close()
    return deleted


def update_match_goal_author(
    goal_id: int,
    player_id: int | None,
    side: str | None = None,
    raw_name: str | None = None,
) -> bool:
    """Reassign the author of a single goal. Returns True if updated.

    ``side`` and ``raw_name`` are optional — only fields that are not
    ``None`` get written. Pass ``raw_name=""`` to clear the raw OCR
    label.
    """
    sets: list[str] = ["player_id=?"]
    vals: list = [player_id]
    if side is not None:
        sets.append("side=?")
        vals.append(side)
    if raw_name is not None:
        cleaned = raw_name.strip() or None
        sets.append("raw_name=?")
        vals.append(cleaned)
    vals.append(goal_id)

    conn = get_conn()
    cur = conn.execute(
        f"UPDATE match_goals SET {', '.join(sets)} WHERE id=?", vals
    )
    updated = bool(getattr(cur, "rowcount", 0) or 0)
    conn.commit()
    conn.close()
    return updated


# ── Bans ──────────────────────────────────────────────────────────────────────

def ban_player(player_id: int, until: str | None, reason: str | None = None):
    """
    until: ISO-8601 datetime string ("YYYY-MM-DD HH:MM:SS"). None = permanent ban.
    """
    if until is None:
        until = "9999-12-31 23:59:59"
    conn = get_conn()
    conn.execute(
        "UPDATE players SET banned_until=?, banned_reason=? WHERE id=?",
        (until, reason, player_id),
    )
    conn.commit()
    conn.close()


def unban_player(player_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE players SET banned_until=NULL, banned_reason=NULL WHERE id=?",
        (player_id,),
    )
    conn.commit()
    conn.close()


def is_player_banned(player_or_id) -> bool:
    """Accept a player dict or player_id. Returns True if currently banned."""
    if isinstance(player_or_id, dict):
        until = player_or_id.get("banned_until")
    else:
        p = get_player_by_id(player_or_id)
        until = p["banned_until"] if p else None
    if not until:
        return False
    from datetime import datetime
    # Postgres returns a datetime object directly; SQLite returns a string.
    if isinstance(until, datetime):
        until_dt = until
    else:
        try:
            until_dt = datetime.strptime(str(until), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
    return until_dt > datetime.utcnow()


# ── Manual ELO adjustments (admin only) ──────────────────────────────────────

def adjust_player_elo(player_id: int, delta: float, by_user: str, note: str = ""):
    """Apply +delta or -delta to a player's ELO and record an audit string."""
    from datetime import datetime
    p = get_player_by_id(player_id)
    if not p:
        raise ValueError("Player not found")
    new_elo = p["elo"] + delta
    audit = f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC: {by_user} {delta:+g}"
    if note:
        audit += f" ({note})"
    conn = get_conn()
    conn.execute(
        "UPDATE players SET elo=?, last_elo_adjust=? WHERE id=?",
        (new_elo, audit, player_id),
    )
    conn.commit()
    conn.close()
    return new_elo


def set_player_elo(player_id: int, new_elo: float, by_user: str, note: str = ""):
    from datetime import datetime
    p = get_player_by_id(player_id)
    if not p:
        raise ValueError("Player not found")
    audit = f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC: {by_user} = {new_elo:g}"
    if note:
        audit += f" ({note})"
    conn = get_conn()
    conn.execute(
        "UPDATE players SET elo=?, last_elo_adjust=? WHERE id=?",
        (new_elo, audit, player_id),
    )
    conn.commit()
    conn.close()
    return new_elo


# ── Tournament helpers ────────────────────────────────────────────────────────

def create_tournament(
    name: str,
    tournament_type: str = "vsa",
    groups_count: int = 2,
    created_by: int | None = None,
    is_official: bool = True,
    chat_id: str | None = None,
):
    """
    Create a tournament.

    is_official=True  → matches feed the global ELO + ELO_VSA/ELO_RI pools.
    is_official=False → fully isolated leaderboard (only `tournament_elo`).
    chat_id           → optional. When set, screenshots posted in that chat
                        are auto-routed to this tournament.
    """
    if tournament_type not in ("vsa", "ri"):
        raise ValueError(f"Unknown tournament_type: {tournament_type!r}")
    conn = get_conn()
    tid = conn.insert_returning_id(
        """INSERT INTO tournaments
           (name, tournament_type, groups_count, created_by, is_official, chat_id)
           VALUES (?,?,?,?,?,?)""",
        (
            name,
            tournament_type,
            groups_count,
            created_by,
            1 if is_official else 0,
            (str(chat_id) if chat_id is not None else None),
        ),
    )
    conn.commit()
    conn.close()
    return tid


def get_tournament(tid: int):
    conn = get_conn()
    t = conn.execute("SELECT * FROM tournaments WHERE id=?", (tid,)).fetchone()
    conn.close()
    return dict(t) if t else None


def get_active_tournament(tournament_type: str | None = None):
    """Latest non-finished tournament."""
    conn = get_conn()
    if tournament_type:
        t = conn.execute(
            """SELECT * FROM tournaments
               WHERE stage != 'finished' AND tournament_type = ?
               ORDER BY id DESC LIMIT 1""",
            (tournament_type,),
        ).fetchone()
    else:
        t = conn.execute(
            "SELECT * FROM tournaments WHERE stage != 'finished' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    conn.close()
    return dict(t) if t else None


def get_active_tournaments():
    """All non-finished tournaments."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tournaments WHERE stage != 'finished' ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_tournament(tid: int, **kwargs):
    if not kwargs:
        return
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [tid]
    conn.execute(f"UPDATE tournaments SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


def add_player_to_tournament(tid: int, player_id: int, group_name: str):
    """Insert or update a player's group assignment for a tournament.

    Upserts on (tournament_id, player_id): when the row already exists
    (e.g. ``/add_player`` first stored ``group_name='?'`` and then
    ``/start_tournament`` or ``/redraw_groups`` re-runs the draw), the
    ``group_name`` is overwritten with the new value while the cumulative
    group stats columns are preserved. Callers that want to reset stats
    (e.g. ``/redraw_groups``) do that explicitly via SQL UPDATE.
    """
    conn = get_conn()
    conn.execute(
        """INSERT INTO tournament_players
               (tournament_id, player_id, group_name)
           VALUES (?,?,?)
           ON CONFLICT (tournament_id, player_id)
           DO UPDATE SET group_name = excluded.group_name""",
        (tid, player_id, group_name),
    )
    conn.commit()
    conn.close()


def is_player_in_tournament(tid: int, player_id: int) -> bool:
    """Return True if the player has a tournament_players row for ``tid``."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM tournament_players "
        "WHERE tournament_id=? AND player_id=? LIMIT 1",
        (tid, player_id),
    ).fetchone()
    conn.close()
    return row is not None


def remove_player_from_tournament(tid: int, player_id: int) -> bool:
    """Delete a tournament_players row. No-op if the player isn't there.

    SAFETY: this is intended for the **self-signup** flow where players
    leave the lobby before any matches have been drawn. Removing a
    player mid-tournament would orphan their pending matches — for
    that case use ``handlers.admin.cmd_withdraw`` instead.

    Returns ``True`` if a row was actually deleted, ``False`` otherwise.
    """
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM tournament_players "
        "WHERE tournament_id=? AND player_id=?",
        (tid, player_id),
    )
    deleted = bool(getattr(cur, "rowcount", 0))
    conn.commit()
    conn.close()
    return deleted


def get_tournament_players(tid: int):
    conn = get_conn()
    rows = conn.execute(
        """SELECT tp.*, p.username, p.elo, p.telegram_id, p.game_nickname
           FROM tournament_players tp
           JOIN players p ON p.id = tp.player_id
           WHERE tp.tournament_id=?
           ORDER BY tp.group_name, tp.group_points DESC, (tp.group_gf - tp.group_ga) DESC""",
        (tid,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def replace_tournament_player(tid: int, old_pid: int, new_pid: int) -> dict:
    """Swap a player slot in a running tournament.

    Moves the row in ``tournament_players`` (preserving group + group
    stats) and rewrites pending/reported ``matches`` so the new player
    inherits the schedule. Confirmed matches keep ``old_pid`` so ELO and
    historical records stay consistent.

    Returns ``{"matches_moved": <int>}`` for the caller to surface in
    the success message.
    """
    conn = get_conn()
    c = conn.cursor()
    # Move the roster row. We can't UPDATE the PK in-place portably, so
    # delete-then-insert. Wrap in the implicit transaction; if anything
    # below fails, the close-without-commit keeps the DB consistent.
    row = c.execute(
        "SELECT * FROM tournament_players "
        "WHERE tournament_id=? AND player_id=?",
        (tid, old_pid),
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError(
            f"player {old_pid} is not in tournament {tid}"
        )
    row_d = dict(row)
    c.execute(
        "DELETE FROM tournament_players "
        "WHERE tournament_id=? AND player_id=?",
        (tid, old_pid),
    )
    c.execute(
        """INSERT INTO tournament_players
               (tournament_id, player_id, group_name,
                group_points, group_gf, group_ga,
                group_wins, group_draws, group_losses, eliminated)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            tid, new_pid, row_d.get("group_name"),
            row_d.get("group_points") or 0,
            row_d.get("group_gf") or 0,
            row_d.get("group_ga") or 0,
            row_d.get("group_wins") or 0,
            row_d.get("group_draws") or 0,
            row_d.get("group_losses") or 0,
            row_d.get("eliminated") or 0,
        ),
    )

    # Move pending/reported matches. Confirmed ones stay with old player.
    c.execute(
        "UPDATE matches SET player1_id=? "
        "WHERE tournament_id=? AND player1_id=? "
        "AND status IN ('pending','reported')",
        (new_pid, tid, old_pid),
    )
    moved_p1 = c.rowcount or 0
    c.execute(
        "UPDATE matches SET player2_id=? "
        "WHERE tournament_id=? AND player2_id=? "
        "AND status IN ('pending','reported')",
        (new_pid, tid, old_pid),
    )
    moved_p2 = c.rowcount or 0

    # Move isolated tournament_elo row if present (best effort — older
    # installs may not have the table yet, hence the try/except).
    try:
        c.execute(
            "UPDATE tournament_elo SET player_id=? "
            "WHERE tournament_id=? AND player_id=?",
            (new_pid, tid, old_pid),
        )
    except Exception:
        pass

    conn.commit()
    conn.close()
    return {"matches_moved": moved_p1 + moved_p2}


def update_tournament_player(tid: int, player_id: int, **kwargs):
    if not kwargs:
        return
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [tid, player_id]
    conn.execute(
        f"UPDATE tournament_players SET {sets} WHERE tournament_id=? AND player_id=?", vals
    )
    conn.commit()
    conn.close()


def get_tournament_player_tag(tid: int, player_id: int) -> str:
    """Return the team / club tag (``team_tag``) for ``player_id`` in
    tournament ``tid``, or empty string if no tag is set / the player
    isn't in this tournament. Single-row helper for display sites that
    have just a player + tid (and don't already pull the full
    tournament_players row).

    Cheap & cached via the regular get_conn() — callers in render hot
    paths should batch-fetch ``get_tournament_players(tid)`` themselves
    if they need many lookups in a row.
    """
    if not tid or not player_id:
        return ""
    conn = get_conn()
    row = conn.execute(
        "SELECT team_tag FROM tournament_players "
        "WHERE tournament_id=? AND player_id=? LIMIT 1",
        (tid, player_id),
    ).fetchone()
    conn.close()
    if not row:
        return ""
    val = row["team_tag"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
    return (val or "").strip()


def late_join_tournament_group(
    tid: int,
    new_pid: int,
    group_name: str,
    deadline: str | None = None,
) -> dict:
    """Add a player into a running tournament's group and create the
    missing group-stage matches against existing group members.

    * Idempotent on roster: if ``new_pid`` is already in
      ``tournament_players``, the existing row is reused (the group is
      updated only if it was a placeholder ``'?'``).
    * Honours ``tournaments.group_matches_per_pair`` — if the group
      uses a double round-robin (mpp=2), two pending matches per
      opponent are created (alternating home/away).
    * Skips opponent pairs that already have at least one matches row
      (regardless of status), so re-running this command is safe.
    * Initialises the new player's ``tournament_elo`` row at the
      group's current average ELO so they aren't an instant-meal for
      the field.

    Returns a dict ``{"created_match_ids": [...], "skipped_opponents":
    [pid, ...], "group": <group_letter>, "init_elo": <float>}``.
    """
    conn = get_conn()
    c = conn.cursor()

    # Tournament + mpp lookup (used for both fixture generation and
    # the elo bootstrap below).
    t_row = c.execute(
        "SELECT * FROM tournaments WHERE id=?", (tid,)
    ).fetchone()
    if not t_row:
        conn.close()
        raise ValueError(f"tournament {tid} not found")
    t = dict(t_row)
    mpp = max(1, int(t.get("group_matches_per_pair") or 1))

    # Existing roster (group_name + player_id, plus current group_elo
    # average for the bootstrap).
    members = c.execute(
        "SELECT player_id, group_name FROM tournament_players "
        "WHERE tournament_id=?",
        (tid,),
    ).fetchall()
    existing_in_group = [
        m["player_id"] for m in members
        if (m["group_name"] or "") == group_name and m["player_id"] != new_pid
    ]

    # Insert / update the roster row.
    own = next((m for m in members if m["player_id"] == new_pid), None)
    if own is None:
        c.execute(
            """INSERT INTO tournament_players
                   (tournament_id, player_id, group_name)
               VALUES (?,?,?)""",
            (tid, new_pid, group_name),
        )
    elif (own["group_name"] or "?") in ("?", ""):
        c.execute(
            "UPDATE tournament_players SET group_name=? "
            "WHERE tournament_id=? AND player_id=?",
            (group_name, tid, new_pid),
        )
    # else: leave the existing group_name alone (don't accidentally move).

    # Bootstrap tournament_elo at the current group average so the new
    # joiner isn't free ELO for the whole group. Falls back to
    # INITIAL_ELO when the group has no rated players yet.
    avg_row = c.execute(
        """SELECT AVG(te.elo) AS avg_elo
           FROM tournament_elo te
           JOIN tournament_players tp
             ON tp.tournament_id = te.tournament_id
            AND tp.player_id     = te.player_id
          WHERE te.tournament_id = ?
            AND tp.group_name    = ?""",
        (tid, group_name),
    ).fetchone()
    init_elo = (
        float(avg_row["avg_elo"])
        if avg_row and avg_row["avg_elo"] is not None
        else float(INITIAL_ELO)
    )
    c.execute(
        f"""INSERT OR IGNORE INTO tournament_elo (tournament_id, player_id, elo)
           VALUES (?, ?, {INITIAL_ELO})""",
        (tid, new_pid),
    )
    c.execute(
        "UPDATE tournament_elo SET elo=? "
        "WHERE tournament_id=? AND player_id=?",
        (init_elo, tid, new_pid),
    )

    # Create missing group fixtures. Honour mpp (double round-robin
    # alternates home/away on the 2nd leg).
    created: list[int] = []
    skipped: list[int] = []
    for opp in existing_in_group:
        # Already played / scheduled at least once? Skip the entire
        # pair — admins can re-add manually with /admin_addgoal etc.
        existing_pair = c.execute(
            """SELECT id FROM matches
                WHERE tournament_id = ?
                  AND ((player1_id=? AND player2_id=?)
                       OR (player1_id=? AND player2_id=?))
                LIMIT 1""",
            (tid, new_pid, opp, opp, new_pid),
        ).fetchone()
        if existing_pair:
            skipped.append(opp)
            continue
        for leg in range(1, mpp + 1):
            if leg % 2 == 1:
                a, b = new_pid, opp
            else:
                a, b = opp, new_pid
            mid = conn.insert_returning_id(
                """INSERT INTO matches
                       (tournament_id, player1_id, player2_id,
                        stage, round_num, deadline, leg)
                   VALUES (?,?,?,?,?,?,?)""",
                (tid, a, b, "group", leg, deadline, leg),
            )
            created.append(mid)

    conn.commit()
    conn.close()
    return {
        "created_match_ids": created,
        "skipped_opponents": skipped,
        "group":             group_name,
        "init_elo":          init_elo,
    }


# ── Match helpers ─────────────────────────────────────────────────────────────

def create_match(tid, p1_id, p2_id, stage="group", round_num=1, deadline=None, leg=1):
    conn = get_conn()
    mid = conn.insert_returning_id(
        """INSERT INTO matches (tournament_id, player1_id, player2_id, stage, round_num, deadline, leg)
           VALUES (?,?,?,?,?,?,?)""",
        (tid, p1_id, p2_id, stage, round_num, deadline, leg),
    )
    conn.commit()
    conn.close()
    return mid


def get_match(mid: int):
    conn = get_conn()
    m = conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    conn.close()
    return dict(m) if m else None


def find_match_by_screenshot_hash(sha256: str, tid: int | None = None):
    """Return the matches row whose `screenshot_hash` equals `sha256`.
    Optionally scope to a single tournament. Used to reject duplicate
    photo uploads."""
    conn = get_conn()
    if tid is None:
        m = conn.execute(
            "SELECT * FROM matches WHERE screenshot_hash=? LIMIT 1",
            (sha256,),
        ).fetchone()
    else:
        m = conn.execute(
            "SELECT * FROM matches WHERE screenshot_hash=? AND tournament_id=? LIMIT 1",
            (sha256, tid),
        ).fetchone()
    conn.close()
    return dict(m) if m else None


def record_processed_screenshot(
    sha256: str,
    tournament_id: int | None,
    chat_id: str | None,
    match_id: int | None,
    reporter_id: int | None,
):
    """Insert a row into `processed_screenshots`. Idempotent — the
    primary key (sha256, tournament_id) drops duplicates silently."""
    conn = get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO processed_screenshots
           (sha256, tournament_id, chat_id, match_id, reporter_id)
           VALUES (?, ?, ?, ?, ?)""",
        (sha256, tournament_id or 0, str(chat_id or ""), match_id, reporter_id),
    )
    conn.commit()
    conn.close()


def get_processed_screenshot(sha256: str, tournament_id: int | None = None):
    """Return the processed_screenshots row for this hash, or None."""
    conn = get_conn()
    if tournament_id is None:
        r = conn.execute(
            "SELECT * FROM processed_screenshots WHERE sha256=? ORDER BY created_at LIMIT 1",
            (sha256,),
        ).fetchone()
    else:
        r = conn.execute(
            """SELECT * FROM processed_screenshots
               WHERE sha256=? AND tournament_id=? LIMIT 1""",
            (sha256, tournament_id or 0),
        ).fetchone()
    conn.close()
    return dict(r) if r else None


def count_confirmed_matches_between(
    p1_id: int, p2_id: int, tournament_id: int | None = None
) -> dict:
    """
    Count how many confirmed matches each side has won between
    `p1_id` and `p2_id`. Returns ``{"p1_wins": int, "p2_wins": int,
    "draws": int, "total": int}``. Optionally scoped to a tournament.
    """
    conn = get_conn()
    base = """SELECT score1, score2, player1_id, player2_id
              FROM matches
              WHERE status='confirmed'
                AND ((player1_id=? AND player2_id=?)
                     OR (player1_id=? AND player2_id=?))"""
    params = [p1_id, p2_id, p2_id, p1_id]
    if tournament_id:
        base += " AND tournament_id=?"
        params.append(tournament_id)
    rows = conn.execute(base, params).fetchall()
    conn.close()
    p1_wins = p2_wins = draws = 0
    for r in rows:
        s1, s2, a, b = r["score1"], r["score2"], r["player1_id"], r["player2_id"]
        if s1 is None or s2 is None:
            continue
        if s1 == s2:
            draws += 1
            continue
        winner_pid = a if s1 > s2 else b
        if winner_pid == p1_id:
            p1_wins += 1
        elif winner_pid == p2_id:
            p2_wins += 1
    return {
        "p1_wins": p1_wins,
        "p2_wins": p2_wins,
        "draws": draws,
        "total": p1_wins + p2_wins + draws,
    }


def get_pending_match(p1_id, p2_id, tid=None):
    conn = get_conn()
    q = """SELECT * FROM matches
           WHERE status IN ('pending','reported')
             AND ((player1_id=? AND player2_id=?) OR (player1_id=? AND player2_id=?))"""
    params = [p1_id, p2_id, p2_id, p1_id]
    if tid:
        q += " AND tournament_id=?"
        params.append(tid)
    m = conn.execute(q, params).fetchone()
    conn.close()
    return dict(m) if m else None


def update_match(mid: int, **kwargs):
    if not kwargs:
        return
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [mid]
    conn.execute(f"UPDATE matches SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


def get_real_tournament_matches(tid: int, stage: str | None = None) -> list[dict]:
    """Like :func:`get_tournament_matches`, but drops phantom rows.

    Phantom criteria mirror :func:`bot._list_pending_matches_for`:
      • group-stage matches whose two players are NOT in the same group
        of this tournament (per ``tournament_players``);
      • duplicate playoff matches (same pair + stage + leg) are deduped
        by keeping the highest ``id`` (newest insert);
      • rows whose stage is unknown are dropped.

    Use this for anything that drives gameplay (``/simulate``,
    leaderboards, advancement). Keep raw :func:`get_tournament_matches`
    for diagnostic / cleanup tools that need to *see* phantoms.
    """
    raw = get_tournament_matches(tid, stage=stage)
    if not raw:
        return raw

    # Resolve groups for this tournament once.
    conn = get_conn()
    rows = conn.execute(
        "SELECT player_id, group_name FROM tournament_players "
        "WHERE tournament_id=?",
        (tid,),
    ).fetchall()
    conn.close()
    group_map = {r["player_id"]: r["group_name"] for r in rows}

    PLAYOFF = ("r16", "qf", "sf", "final")

    # First pass: drop cross-group / unknown-stage matches.
    intermediate: list[dict] = []
    for m in raw:
        st = m.get("stage")
        if st == "group":
            g1 = group_map.get(m["player1_id"])
            g2 = group_map.get(m["player2_id"])
            if not g1 or not g2 or g1 != g2:
                continue
        elif st not in PLAYOFF:
            continue
        intermediate.append(m)

    # Second pass: dedupe playoff legs by (sorted pair, stage, leg) — keep
    # the highest id (newest). Group-stage rows pass through unchanged.
    best: dict[tuple, dict] = {}
    rest: list[dict] = []
    for m in intermediate:
        if m.get("stage") in PLAYOFF:
            pair = tuple(sorted([m["player1_id"], m["player2_id"]]))
            key = (pair, m["stage"], int(m.get("leg") or 1))
            cur = best.get(key)
            if cur is None or (m["id"] or 0) > (cur["id"] or 0):
                best[key] = m
        else:
            rest.append(m)
    return rest + list(best.values())


def get_tournament_matches(tid: int, stage: str = None):
    conn = get_conn()
    if stage:
        rows = conn.execute(
            "SELECT * FROM matches WHERE tournament_id=? AND stage=? ORDER BY id",
            (tid, stage),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM matches WHERE tournament_id=? ORDER BY id", (tid,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_player_matches(player_id: int, limit: int = 10):
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM matches
           WHERE (player1_id=? OR player2_id=?) AND status='confirmed'
           ORDER BY played_at DESC LIMIT ?""",
        (player_id, player_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_overdue_matches():
    """Pending matches whose deadline already passed.

    Matches that belong to a finished tournament are explicitly excluded
    so the bot doesn't keep sending walkover / reminder messages after
    the tournament was closed via ``/finish_tournament``. Friendly
    matches (``tournament_id IS NULL`` or ``=0``) are still returned.
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT m.* FROM matches m
      LEFT JOIN tournaments t ON t.id = m.tournament_id
          WHERE m.status='pending'
            AND m.deadline < datetime('now')
            AND (t.id IS NULL OR COALESCE(t.stage,'') != 'finished')""",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_upcoming_deadline_matches(hours=6):
    """Pending matches whose deadline is within ``hours`` from now.

    Same filter as :func:`get_overdue_matches`: skips finished
    tournaments so reminders stop after the tournament is closed.
    """
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT m.* FROM matches m
      LEFT JOIN tournaments t ON t.id = m.tournament_id
          WHERE m.status='pending'
            AND m.deadline BETWEEN datetime('now') AND datetime('now', '+{hours} hours')
            AND (t.id IS NULL OR COALESCE(t.stage,'') != 'finished')""",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Per-tournament isolated ELO (player-created tournaments) ─────────────────

def get_tournament_elo(tid: int, player_id: int) -> dict:
    """
    Return the per-tournament ELO row for `player_id` in tournament `tid`.
    If no row exists yet, return a default row at INITIAL_ELO with zeroed
    counters (so callers can compute a brand-new player's first delta).
    """
    conn = get_conn()
    r = conn.execute(
        "SELECT * FROM tournament_elo WHERE tournament_id=? AND player_id=?",
        (tid, player_id),
    ).fetchone()
    conn.close()
    if r:
        return dict(r)
    return {
        "tournament_id": tid,
        "player_id": player_id,
        "elo": float(INITIAL_ELO),
        "games": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "goals_for": 0,
        "goals_against": 0,
    }


def upsert_tournament_elo(tid: int, player_id: int, **kwargs):
    """
    Ensure a row exists in `tournament_elo` for (tid, player_id), then update
    the supplied fields. Use this from match_processor after computing a delta.
    """
    conn = get_conn()
    conn.execute(
        f"""INSERT OR IGNORE INTO tournament_elo (tournament_id, player_id, elo)
           VALUES (?, ?, {INITIAL_ELO})""",
        (tid, player_id),
    )
    if kwargs:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [tid, player_id]
        conn.execute(
            f"UPDATE tournament_elo SET {sets} "
            f"WHERE tournament_id=? AND player_id=?",
            vals,
        )
    conn.commit()
    conn.close()


def get_tournament_leaderboard(tid: int) -> list[dict]:
    """
    Return all rows from tournament_elo for `tid`, joined with player info,
    sorted by ELO desc (then GD desc, GF desc).

    Players who joined the tournament but haven't played a confirmed match yet
    are also included with elo=INITIAL_ELO so the leaderboard shows everyone.
    """
    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(te.elo,           {INITIAL_ELO}) AS elo,
            COALESCE(te.games,         0)             AS games,
            COALESCE(te.wins,          0)             AS wins,
            COALESCE(te.draws,         0)             AS draws,
            COALESCE(te.losses,        0)             AS losses,
            COALESCE(te.goals_for,     0)             AS goals_for,
            COALESCE(te.goals_against, 0)             AS goals_against,
            tp.player_id                                AS player_id,
            p.username                                  AS username,
            p.game_nickname                             AS game_nickname,
            p.telegram_id                               AS telegram_id
        FROM tournament_players tp
        JOIN players p ON p.id = tp.player_id
        LEFT JOIN tournament_elo te
            ON te.tournament_id = tp.tournament_id
           AND te.player_id     = tp.player_id
        WHERE tp.tournament_id = ?
        ORDER BY elo DESC,
                 (goals_for - goals_against) DESC,
                 goals_for DESC
        """,
        (tid,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Bot-admin promotion (runtime grants) ─────────────────────────────────────

def grant_bot_admin(
    telegram_id: int,
    granted_by: int | None = None,
    note: str | None = None,
) -> None:
    """
    Promote a Telegram user to bot admin. Idempotent — granting an existing
    admin updates the audit fields. The env-var ADMIN_IDS list is unaffected
    and still acts as the "root" admin set.
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO bot_admins (telegram_id, granted_by, note)
           VALUES (?, ?, ?)
           ON CONFLICT(telegram_id) DO UPDATE SET
               granted_by = excluded.granted_by,
               granted_at = CURRENT_TIMESTAMP,
               note       = excluded.note""",
        (int(telegram_id), int(granted_by) if granted_by is not None else None, note),
    )
    conn.commit()
    conn.close()


def revoke_bot_admin(telegram_id: int) -> bool:
    """
    Remove a runtime-granted admin. Returns True if a row was removed.
    Has no effect on env-var ADMIN_IDS — those have to be removed at the
    deployment level.
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM bot_admins WHERE telegram_id=?", (int(telegram_id),))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


def is_bot_admin_db(telegram_id: int) -> bool:
    """True if `telegram_id` is in the runtime bot_admins table."""
    if telegram_id is None:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM bot_admins WHERE telegram_id=?", (int(telegram_id),)
    ).fetchone()
    conn.close()
    return row is not None


def list_bot_admins() -> list[dict]:
    """Return all runtime-promoted admins, newest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_admins ORDER BY granted_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Bot-owner promotion (super-admin grants) ─────────────────────────────────

def grant_bot_owner(
    telegram_id: int,
    granted_by: int | None = None,
    note: str | None = None,
) -> None:
    """Promote a Telegram user to bot owner (super-admin). Idempotent."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO bot_owners (telegram_id, granted_by, note)
           VALUES (?, ?, ?)
           ON CONFLICT(telegram_id) DO UPDATE SET
               granted_by = excluded.granted_by,
               granted_at = CURRENT_TIMESTAMP,
               note       = excluded.note""",
        (int(telegram_id), int(granted_by) if granted_by is not None else None, note),
    )
    conn.commit()
    conn.close()


def revoke_bot_owner(telegram_id: int) -> bool:
    """Remove a bot owner. Returns True if a row was removed."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM bot_owners WHERE telegram_id=?", (int(telegram_id),))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


def is_bot_owner_db(telegram_id: int) -> bool:
    """True if telegram_id is in the bot_owners table."""
    if telegram_id is None:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM bot_owners WHERE telegram_id=?", (int(telegram_id),)
    ).fetchone()
    conn.close()
    return row is not None


def list_bot_owners() -> list[dict]:
    """Return all bot owners, newest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bot_owners ORDER BY granted_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Tournament-scoped admin delegation ──────────────────────────────────────

def add_tournament_admin(
    tournament_id: int,
    telegram_id: int,
    granted_by: int | None = None,
    note: str | None = None,
) -> None:
    """
    Add ``telegram_id`` to the admin list for ``tournament_id``. Idempotent —
    re-adding the same user updates the audit fields. The creator of a
    tournament is *implicitly* a tournament admin and does NOT need a row
    here.
    """
    conn = get_conn()
    conn.execute(
        """INSERT INTO tournament_admins
               (tournament_id, telegram_id, granted_by, note)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(tournament_id, telegram_id) DO UPDATE SET
               granted_by = excluded.granted_by,
               granted_at = CURRENT_TIMESTAMP,
               note       = excluded.note""",
        (int(tournament_id), int(telegram_id),
         int(granted_by) if granted_by is not None else None, note),
    )
    conn.commit()
    conn.close()


def remove_tournament_admin(tournament_id: int, telegram_id: int) -> bool:
    """Remove a per-tournament admin row. Returns True if a row was deleted."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM tournament_admins "
        "WHERE tournament_id=? AND telegram_id=?",
        (int(tournament_id), int(telegram_id)),
    )
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


def is_tournament_admin(tournament_id: int, telegram_id: int | None) -> bool:
    """True if ``telegram_id`` was explicitly delegated for ``tournament_id``.

    Does NOT consider the creator or root admins — callers that want the
    full "can manage" check should also OR-in those conditions (see
    ``bot._can_manage_tournament``).
    """
    if telegram_id is None:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM tournament_admins "
        "WHERE tournament_id=? AND telegram_id=? LIMIT 1",
        (int(tournament_id), int(telegram_id)),
    ).fetchone()
    conn.close()
    return row is not None


def list_tournament_admins(tournament_id: int) -> list[dict]:
    """All explicitly-delegated admins of a tournament, newest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tournament_admins "
        "WHERE tournament_id=? ORDER BY granted_at DESC",
        (int(tournament_id),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_tournament_admin_for_user(telegram_id: int) -> list[int]:
    """List of tournament_ids ``telegram_id`` was delegated as admin for."""
    if telegram_id is None:
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT tournament_id FROM tournament_admins WHERE telegram_id=?",
        (int(telegram_id),),
    ).fetchall()
    conn.close()
    return [int(r["tournament_id"]) for r in rows]


# ── Chat ↔ tournament binding ────────────────────────────────────────────────

def set_tournament_chat(tid: int, chat_id: str | int) -> None:
    """
    Bind tournament `tid` to Telegram chat `chat_id` (stored as TEXT).
    Ensures a 1:1 chat→tournament relationship: any other tournament
    previously bound to the same chat is unbound first.
    """
    conn = get_conn()
    conn.execute(
        "UPDATE tournaments SET chat_id=NULL WHERE chat_id=? AND id != ?",
        (str(chat_id), tid),
    )
    conn.execute(
        "UPDATE tournaments SET chat_id=? WHERE id=?",
        (str(chat_id), tid),
    )
    conn.commit()
    conn.close()


def unset_tournament_chat(tid: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE tournaments SET chat_id=NULL WHERE id=?", (tid,))
    conn.commit()
    conn.close()


def get_tournament_by_chat(chat_id: str | int) -> dict | None:
    """
    Return the most recent non-finished tournament bound to `chat_id`, or any
    bound tournament if none active. None if nothing is bound.
    """
    conn = get_conn()
    t = conn.execute(
        """SELECT * FROM tournaments
           WHERE chat_id = ?
           ORDER BY (CASE WHEN stage = 'finished' THEN 1 ELSE 0 END), id DESC
           LIMIT 1""",
        (str(chat_id),),
    ).fetchone()
    conn.close()
    return dict(t) if t else None


def find_tournaments_by_name_substring(query: str) -> list[dict]:
    """
    Case-insensitive substring search over tournament names. Used by the
    photo handler to resolve captions like 'тур Гвардиолыча'. Empty / None
    query returns []. Results are ordered: non-finished first, then by id desc.

    Filtering is done in Python (not via SQL `LOWER(...) LIKE`) because
    SQLite's built-in LOWER() only handles ASCII — passing Cyrillic through
    it leaves the string unchanged and the LIKE never matches.
    """
    if not query or not query.strip():
        return []
    q = query.strip().lower()
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM tournaments
           ORDER BY (CASE WHEN stage = 'finished' THEN 1 ELSE 0 END), id DESC
           LIMIT 500"""
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        name = (d.get("name") or "").lower()
        if q in name:
            out.append(d)
            if len(out) >= 10:
                break
    return out


# ── Tournament audit log ─────────────────────────────────────────────────────

def log_tournament_action(
    tournament_id: int | None,
    *,
    actor_telegram_id: int | None,
    actor_username: str | None,
    action: str,
    details: str | None = None,
) -> None:
    """Append a row to ``tournament_audit_log``.

    No-op (silently) if ``tournament_id`` is None — keeps call sites
    terse for actions that may or may not be tournament-scoped.
    """
    if tournament_id is None:
        return
    try:
        conn = get_conn()
        conn.execute(
            """INSERT INTO tournament_audit_log
                 (tournament_id, actor_telegram_id, actor_username,
                  action, details)
               VALUES (?, ?, ?, ?, ?)""",
            (
                int(tournament_id),
                int(actor_telegram_id) if actor_telegram_id is not None else None,
                actor_username,
                action,
                details,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        # Audit logging must never crash the calling flow.
        pass


def list_tournament_audit_log(
    tournament_id: int, limit: int = 30
) -> list[dict]:
    """Return the most recent ``limit`` audit rows for the tournament,
    newest first."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM tournament_audit_log
           WHERE tournament_id=?
           ORDER BY id DESC
           LIMIT ?""",
        (int(tournament_id), int(limit)),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_tournaments(limit: int = 15) -> list[dict]:
    """Return the most recent tournaments (active first, then finished),
    ordered by id DESC. Used for interactive /audit tournament selection."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM tournaments
           ORDER BY (CASE WHEN stage = 'finished' THEN 1 ELSE 0 END),
                    id DESC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_audit_distinct_actors(tournament_id: int) -> list[dict]:
    """Return distinct actors from the audit log of a tournament.
    Each entry has actor_telegram_id and actor_username.
    Useful for the 'filter by admin' UI."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT actor_telegram_id, actor_username
           FROM tournament_audit_log
           WHERE tournament_id=? AND actor_username IS NOT NULL
           ORDER BY actor_username""",
        (int(tournament_id),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Open-match queries (used by /my_deadlines, /withdraw) ────────────────────

def get_open_matches_for_player(
    player_id: int, tournament_id: int | None = None
) -> list[dict]:
    """All non-confirmed matches involving ``player_id``, ordered by
    deadline (NULLs last). Optionally restrict to a single tournament.
    """
    conn = get_conn()
    sql = (
        "SELECT * FROM matches "
        "WHERE (player1_id=? OR player2_id=?) "
        "  AND status IN ('pending','reported') "
    )
    params: list = [int(player_id), int(player_id)]
    if tournament_id is not None:
        sql += "  AND tournament_id=? "
        params.append(int(tournament_id))
    sql += (
        "ORDER BY (deadline IS NULL), deadline ASC, id ASC"
    )
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_existing_group_match(p1_id: int, p2_id: int, tid: int):
    """Find an existing group-stage match between two players in a tournament
    regardless of status (confirmed, awaiting_admin, etc.). Returns the
    newest such match or None."""
    conn = get_conn()
    m = conn.execute(
        """SELECT * FROM matches
           WHERE tournament_id = ?
             AND stage = 'group'
             AND ((player1_id=? AND player2_id=?)
                  OR (player1_id=? AND player2_id=?))
           ORDER BY id DESC LIMIT 1""",
        (tid, p1_id, p2_id, p2_id, p1_id),
    ).fetchone()
    conn.close()
    return dict(m) if m else None


def count_group_matches_for_pair(p1_id: int, p2_id: int, tid: int) -> int:
    """Count all non-cancelled group-stage matches between two players
    in a tournament (any status except deleted/cancelled)."""
    conn = get_conn()
    row = conn.execute(
        """SELECT COUNT(*) AS cnt FROM matches
           WHERE tournament_id = ?
             AND stage = 'group'
             AND ((player1_id=? AND player2_id=?)
                  OR (player1_id=? AND player2_id=?))""",
        (tid, p1_id, p2_id, p2_id, p1_id),
    ).fetchone()
    conn.close()
    if not row:
        return 0
    return row["cnt"] if isinstance(row, dict) else row[0]


def get_h2h_matches(player_a_id: int, player_b_id: int) -> list[dict]:
    """All confirmed matches between two players, newest first."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM matches
           WHERE status='confirmed'
             AND ((player1_id=? AND player2_id=?)
                  OR (player1_id=? AND player2_id=?))
           ORDER BY COALESCE(played_at, created_at) DESC, id DESC""",
        (
            int(player_a_id), int(player_b_id),
            int(player_b_id), int(player_a_id),
        ),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]



# ── Player titles / awards ───────────────────────────────────────────────────

def add_player_title(
    player_id: int,
    title: str,
    *,
    granted_by: int | None = None,
    note: str | None = None,
) -> int:
    """Insert a new title for ``player_id``. Returns the new row id.

    Multiple identical titles are allowed (admin can re-award the same
    title with a different note). Use ``remove_player_title`` to drop
    by id, or ``remove_player_title_by_text`` to drop by exact title
    match.
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    conn = get_conn()
    new_id = conn.insert_returning_id(
        "INSERT INTO player_titles (player_id, title, granted_by, note) "
        "VALUES (?, ?, ?, ?)",
        (int(player_id), title[:120], granted_by, note),
    )
    conn.commit()
    conn.close()
    return int(new_id)


def list_player_titles(player_id: int) -> list[dict]:
    """Return every title for ``player_id`` in newest-first order."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, player_id, title, granted_by, granted_at, note "
        "FROM player_titles WHERE player_id=? "
        "ORDER BY granted_at DESC, id DESC",
        (int(player_id),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_player_title_by_text(player_id: int, title: str) -> int:
    """Delete every row of ``player_titles`` for ``(player_id, title)``.

    Match is case-insensitive (Python ``str.lower()`` so Cyrillic works
    correctly — the SQLite ``LOWER()`` builtin only handles ASCII).
    Returns the number of rows removed (0 if no such title was on the
    player).
    """
    title_norm = (title or "").strip().lower()
    if not title_norm:
        return 0
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title FROM player_titles WHERE player_id=?",
        (int(player_id),),
    ).fetchall()
    ids_to_remove = [
        r["id"] for r in rows
        if (r["title"] or "").strip().lower() == title_norm
    ]
    removed = 0
    for tid_row in ids_to_remove:
        conn.execute(
            "DELETE FROM player_titles WHERE id=?", (int(tid_row),),
        )
        removed += 1
    conn.commit()
    conn.close()
    return removed


def player_title_strings(player_id: int) -> list[str]:
    """Return just the title text for ``player_id`` — convenience helper
    for renderers that build ``"<nick> [🐐 GOAT, Чемпион]"`` lines."""
    return [t["title"] for t in list_player_titles(player_id)]


# ── Quotes & per-chat settings ──────────────────────────────────────────────

def add_quote(
    text: str,
    *,
    author: str | None = None,
    chat_id: str | None = None,
    added_by: int | None = None,
    voice_file_id: str | None = None,
) -> int:
    """Persist a new quote. Returns the row id."""
    text = (text or "").strip()
    if not text and not voice_file_id:
        raise ValueError("text or voice_file_id is required")
    conn = get_conn()
    new_id = conn.insert_returning_id(
        "INSERT INTO quotes (chat_id, text, author, added_by, voice_file_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            str(chat_id) if chat_id is not None else None,
            text[:2000] if text else None,
            (author or "").strip()[:120] or None,
            added_by,
            voice_file_id,
        ),
    )
    conn.commit()
    conn.close()
    return int(new_id)


def list_quotes(
    chat_id: str | int | None = None, limit: int = 30,
) -> list[dict]:
    """List quotes for ``chat_id`` (or all chats when None), newest first."""
    conn = get_conn()
    if chat_id is not None:
        rows = conn.execute(
            "SELECT id, chat_id, text, author, added_by, added_at, voice_file_id "
            "FROM quotes WHERE chat_id=? "
            "ORDER BY id DESC LIMIT ?",
            (str(chat_id), int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, chat_id, text, author, added_by, added_at, voice_file_id "
            "FROM quotes ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_quote(quote_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM quotes WHERE id=?", (int(quote_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def random_quote_for_chat(chat_id: str | int) -> dict | None:
    """Pick a uniformly random quote for the chat. Returns None if
    none exist for this chat (and the bot won't post anything)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, chat_id, text, author, added_by, added_at, voice_file_id "
        "FROM quotes WHERE chat_id=? "
        "ORDER BY RANDOM() LIMIT 1",
        (str(chat_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_quote(quote_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM quotes WHERE id=?", (int(quote_id),),
    )
    removed = bool(getattr(cur, "rowcount", 0) or 0)
    conn.commit()
    conn.close()
    return removed


def get_chat_settings(chat_id: str | int) -> dict:
    """Fetch the chat_settings row for ``chat_id`` (creating defaults
    when missing).

    Default quiet hours are 23..12 in the operator's display TZ — the
    quote loop stays silent at night so the bot doesn't ping at 3 AM.
    Admins can override via :func:`set_chat_quote_quiet_hours` (same
    settings panel button).
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM chat_settings WHERE chat_id=?",
        (str(chat_id),),
    ).fetchone()
    conn.close()
    if row:
        d = dict(row)
        # Backfill defaults so callers don't have to handle NULLs from
        # rows that pre-date the quiet-hour columns.
        if d.get("quiet_start_hour") is None:
            d["quiet_start_hour"] = 23
        if d.get("quiet_end_hour") is None:
            d["quiet_end_hour"] = 12
        return d
    return {
        "chat_id": str(chat_id),
        "quote_interval_minutes": 0,
        "last_quote_at": None,
        "quiet_start_hour": 23,
        "quiet_end_hour": 12,
    }


def set_chat_quote_quiet_hours(
    chat_id: str | int, start_hour: int, end_hour: int,
) -> None:
    """Upsert quiet-hour window (in display TZ). Both bounds clamped
    to ``0..23``. ``start == end`` disables quiet hours (24/7 quotes).
    """
    start_hour = max(0, min(23, int(start_hour)))
    end_hour = max(0, min(23, int(end_hour)))
    conn = get_conn()
    cur = conn.execute(
        "UPDATE chat_settings SET quiet_start_hour=?, quiet_end_hour=? "
        "WHERE chat_id=?",
        (start_hour, end_hour, str(chat_id)),
    )
    if not getattr(cur, "rowcount", 0):
        conn.execute(
            "INSERT INTO chat_settings "
            "(chat_id, quote_interval_minutes, quiet_start_hour, quiet_end_hour) "
            "VALUES (?, 0, ?, ?)",
            (str(chat_id), start_hour, end_hour),
        )
    conn.commit()
    conn.close()


def set_chat_quote_interval(chat_id: str | int, minutes: int) -> None:
    """Upsert ``quote_interval_minutes`` for ``chat_id``."""
    minutes = max(0, int(minutes))
    conn = get_conn()
    # Try update first; if no row, insert.
    cur = conn.execute(
        "UPDATE chat_settings SET quote_interval_minutes=? WHERE chat_id=?",
        (minutes, str(chat_id)),
    )
    if not getattr(cur, "rowcount", 0):
        conn.execute(
            "INSERT INTO chat_settings (chat_id, quote_interval_minutes) "
            "VALUES (?, ?)",
            (str(chat_id), minutes),
        )
    conn.commit()
    conn.close()


def mark_chat_quote_sent(chat_id: str | int) -> None:
    """Update ``last_quote_at`` to UTC-now after the bot posts a quote
    so the loop throttles correctly."""
    from datetime import datetime as _dt  # local import: keep helper standalone
    conn = get_conn()
    now = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "UPDATE chat_settings SET last_quote_at=? WHERE chat_id=?",
        (now, str(chat_id)),
    )
    if not getattr(cur, "rowcount", 0):
        conn.execute(
            "INSERT INTO chat_settings "
            "(chat_id, quote_interval_minutes, last_quote_at) "
            "VALUES (?, 0, ?)",
            (str(chat_id), now),
        )
    conn.commit()
    conn.close()


def list_chats_with_quote_interval() -> list[dict]:
    """All chats with ``quote_interval_minutes > 0``. Used by the
    background quote loop. Includes the quiet-hour bounds so the
    loop can skip nights without an extra round-trip per chat.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT chat_id, quote_interval_minutes, last_quote_at, "
        "       quiet_start_hour, quiet_end_hour "
        "FROM chat_settings WHERE quote_interval_minutes > 0"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
