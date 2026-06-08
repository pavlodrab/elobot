"""Tests for the inline "🏆 Создать кубки" UX added on top of
``spawn_cl_followup_cups``:

* the ``followup_cups_config`` JSON column is round-tripped via the
  template → tournaments row,
* ``parse_followup_cups_config`` decodes the saved JSON,
* ``spawn_cl_followup_cups`` records ``followup_cups_tids`` on the
  league row so the second call can refuse cleanly,
* ``submenu_tournament_settings`` swaps the "Создать кубки" button
  for direct "Открыть основной/Лига Конфети" links once the cups
  exist.

The button itself is rendered by ``bot.submenu_tournament_settings``;
we only check the callback strings since rendering is one-to-one with
the data layer here.
"""
from __future__ import annotations

import os
import sys
import tempfile


def _setup():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="cl_button_test_")
    os.close(fd)
    os.environ["DB_PATH"] = path
    os.environ.setdefault("BOT_TOKEN", "test")
    for mod in [m for m in list(sys.modules)
                if m in ("database", "tournament", "tournament_templates",
                         "bot", "match_processor")
                or m.startswith(("database.", "tournament.", "bot.",
                                 "handlers."))]:
        del sys.modules[mod]
    import database as db
    import tournament as t
    import tournament_templates as tt
    db.init_db()
    return db, t, tt, path


def _seed_finished_league(db, t, n: int = 32):
    pids = []
    for i in range(n):
        p = db.upsert_player(f"x{i:02d}", telegram_id=80000 + i)
        db.set_player_elo(p["id"], 2000 - i, by_user="test")
        pids.append(p["id"])
    creator = pids[0]
    league_tid = db.create_tournament(
        "CL Button", tournament_type="vsa", created_by=creator,
    )
    db.update_tournament(
        league_tid,
        groups_only=1,
        groups_count=1,
        group_matches_per_pair=1,
        playoff_third_place=0,
        followup_cups_config=(
            '{"main_size": 24, "consolation_size": 8, "legs_per_pair": 2}'
        ),
    )
    for pid in pids:
        db.add_player_to_tournament(league_tid, pid, "A")
    t.generate_group_fixtures(league_tid, {"A": pids})
    for m in db.get_tournament_matches(league_tid, stage="group"):
        i1 = pids.index(m["player1_id"])
        i2 = pids.index(m["player2_id"])
        if i1 < i2:
            db.update_match(m["id"], score1=3, score2=0, status="confirmed")
        else:
            db.update_match(m["id"], score1=0, score2=3, status="confirmed")
    db.update_tournament(league_tid, stage="groups_done")
    return league_tid, pids


def test_template_persists_followup_cups_config():
    db, t, tt, path = _setup()
    try:
        # The built-in CL-32 template must carry the JSON that
        # downstream code parses for sizes.
        tpl = tt.get_builtin_template("champions_league_32")
        assert tpl is not None, "built-in champions_league_32 template missing"
        kwargs = tpl.to_tournament_kwargs()
        assert kwargs.get("followup_cups_config"), (
            "template kwargs must include followup_cups_config"
        )
        cfg = t.parse_followup_cups_config(kwargs["followup_cups_config"])
        # consolation_size is intentionally omitted from the template
        # JSON — spawner derives it from the actual roster, so the
        # same template handles 32, 34, 36 … players out of the box.
        assert cfg == {
            "main_size": 24,
            "consolation_size": None,
            "legs_per_pair": 2,
        }, f"unexpected parsed cfg: {cfg}"
    finally:
        os.unlink(path)


def test_parse_followup_cups_config_handles_garbage():
    db, t, tt, path = _setup()
    try:
        assert t.parse_followup_cups_config(None) is None
        assert t.parse_followup_cups_config("") is None
        assert t.parse_followup_cups_config("not-json") is None
        assert t.parse_followup_cups_config("[]") is None  # not a dict
        # Partial config falls back to defaults; missing
        # consolation_size stays as None (auto-derive).
        cfg = t.parse_followup_cups_config('{"main_size": 16}')
        assert cfg == {
            "main_size": 16,
            "consolation_size": None,
            "legs_per_pair": 2,
        }, f"unexpected parsed cfg: {cfg}"
        # Explicit consolation_size is honoured.
        cfg = t.parse_followup_cups_config(
            '{"main_size": 24, "consolation_size": 8}'
        )
        assert cfg["consolation_size"] == 8
    finally:
        os.unlink(path)


def test_spawn_records_tids_and_refuses_double_spawn():
    db, t, tt, path = _setup()
    try:
        league_tid, _pids = _seed_finished_league(db, t)
        result = t.spawn_cl_followup_cups(league_tid)
        assert result["main_tid"] and result["consolation_tid"]

        # League row must now know its children — used by the panel
        # to swap the spawn button for direct links.
        league = db.get_tournament(league_tid)
        tids = t.parse_followup_cups_tids(league.get("followup_cups_tids"))
        assert tids == (result["main_tid"], result["consolation_tid"])

        # Second call must refuse, NOT spawn duplicate cups.
        try:
            t.spawn_cl_followup_cups(league_tid)
        except ValueError as e:
            assert "already spawned" in str(e)
        else:
            raise AssertionError(
                "expected ValueError on double-spawn, got success"
            )
    finally:
        os.unlink(path)


def test_settings_panel_shows_spawn_button_then_links():
    """Render ``submenu_tournament_settings`` for the league and check
    the keyboard's first-row callback_data flips from
    ``ts:cl_spawn:<tid>`` to ``ts:open:<main>`` / ``ts:open:<cons>``
    after spawning.
    """
    db, t, tt, path = _setup()
    try:
        league_tid, _pids = _seed_finished_league(db, t)
        from bot import submenu_tournament_settings

        league = db.get_tournament(league_tid)
        kb = submenu_tournament_settings(league)
        flat = [
            (btn.text, btn.callback_data)
            for row in kb.inline_keyboard for btn in row
        ]
        spawn_cbs = [cb for _txt, cb in flat if cb and cb.startswith("ts:cl_spawn:")]
        assert spawn_cbs == [f"ts:cl_spawn:{league_tid}"], (
            f"finished league with config must offer one cl_spawn button, got {flat}"
        )

        # Spawn the cups, re-render — button should disappear, replaced
        # by direct links to the new tournaments.
        result = t.spawn_cl_followup_cups(league_tid)
        league = db.get_tournament(league_tid)
        kb = submenu_tournament_settings(league)
        flat = [
            (btn.text, btn.callback_data)
            for row in kb.inline_keyboard for btn in row
        ]
        spawn_cbs = [cb for _txt, cb in flat if cb and cb.startswith("ts:cl_spawn:")]
        assert spawn_cbs == [], (
            "after spawning, the cl_spawn button must be gone"
        )
        opens = [cb for _txt, cb in flat if cb and cb.startswith("ts:open:")]
        assert f"ts:open:{result['main_tid']}" in opens
        assert f"ts:open:{result['consolation_tid']}" in opens
    finally:
        os.unlink(path)


def test_settings_panel_no_button_for_unrelated_league():
    """A regular league with no ``followup_cups_config`` must NOT show
    the spawn button — this isn't a generic feature, only the CL-32
    template opts in.
    """
    db, t, tt, path = _setup()
    try:
        # Create a normal 4-player league, no followup config.
        pids = [db.upsert_player(f"y{i:02d}", telegram_id=90000 + i)["id"] for i in range(4)]
        creator = pids[0]
        tid = db.create_tournament("Plain League", tournament_type="vsa", created_by=creator)
        db.update_tournament(tid, groups_only=1, groups_count=1, stage="groups_done")
        for pid in pids:
            db.add_player_to_tournament(tid, pid, "A")

        from bot import submenu_tournament_settings
        kb = submenu_tournament_settings(db.get_tournament(tid))
        flat = [
            (btn.text, btn.callback_data)
            for row in kb.inline_keyboard for btn in row
        ]
        spawn_cbs = [cb for _txt, cb in flat if cb and cb.startswith("ts:cl_spawn")]
        assert spawn_cbs == [], (
            f"plain league must not show cl_spawn button, got {flat}"
        )
    finally:
        os.unlink(path)


def main() -> int:
    failures: list[str] = []
    for fn in [
        test_template_persists_followup_cups_config,
        test_parse_followup_cups_config_handles_garbage,
        test_spawn_records_tids_and_refuses_double_spawn,
        test_settings_panel_shows_spawn_button_then_links,
        test_settings_panel_no_button_for_unrelated_league,
    ]:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failures.append(fn.__name__)
        except Exception as e:
            import traceback
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failures.append(fn.__name__)
    if failures:
        print(f"\n{len(failures)} test(s) failed.")
        return 1
    print("\nAll cl_spawn button-flow tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
