"""
Tournament template system.

Provides pre-built and custom tournament templates that encapsulate all
the settings needed to create a tournament in one click. Templates can be:

- **Built-in**: hardcoded presets (League, Cup, Groups+Playoff, etc.)
- **Custom**: user-created templates stored in the DB with full
  tournament configuration.

A template is just a dict of tournament settings that gets applied to a
newly-created tournament row via ``database.update_tournament``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Template data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TournamentTemplate:
    """Describes all configurable parameters for a tournament."""

    # Identity
    name: str = ""
    description: str = ""
    template_type: str = "custom"  # "league", "cup", "groups_playoff", "custom"

    # Game type: "vsa" or "ri"
    tournament_type: str = "vsa"

    # Structure
    bracket_only: int = 0        # 1 = knockout without groups
    groups_only: int = 0         # 1 = round-robin only, no playoff
    groups_count: int = 1        # number of groups
    target_group_size: Optional[int] = None  # players per group (auto if None)
    playoff_slots: int = 2       # how many advance per group to playoff

    # Match settings
    group_matches_per_pair: int = 1   # 1 = single RR, 2 = double RR
    playoff_matches_per_pair: int = 1  # legs per playoff tie
    series_length: int = 0       # best-of-N (0/1 = single match)
    playoff_advance_mode: str = "goals"  # "goals" or "wins"
    playoff_third_place: int = 1  # bronze match on/off

    # Draw mode for cup/playoff bracket
    draw_mode: str = "auto"  # "auto" (seeded by ELO), "random", "manual"

    # Tech loss
    auto_tech_loss_enabled: int = 0
    auto_tech_loss_score: str = "0:3"

    # Misc
    auto_confirm: int = 0
    open_signup: int = 1
    reminder_dm_hours: int = 12
    reminder_chat_enabled: int = 0

    # Playoff stage config (JSON string)
    playoff_stage_config: str = "{}"

    # Tours (rounds)
    tours_enabled: int = 0
    total_tours: int = 0
    auto_next_tour: int = 0

    # Custom display name for the single group in a league (Лига ЧМ, Сетка 1)
    group_display_name: str = ""

    def to_tournament_kwargs(self) -> dict:
        """Convert template to kwargs for create_tournament + update_tournament."""
        return {
            "bracket_only": self.bracket_only,
            "groups_only": self.groups_only,
            "groups_count": self.groups_count,
            "target_group_size": self.target_group_size,
            "playoff_slots": self.playoff_slots,
            "group_matches_per_pair": self.group_matches_per_pair,
            "playoff_matches_per_pair": self.playoff_matches_per_pair,
            "series_length": self.series_length,
            "playoff_advance_mode": self.playoff_advance_mode,
            "playoff_third_place": self.playoff_third_place,
            "auto_tech_loss_enabled": self.auto_tech_loss_enabled,
            "auto_tech_loss_score": self.auto_tech_loss_score,
            "auto_confirm": self.auto_confirm,
            "open_signup": self.open_signup,
            "reminder_dm_hours": self.reminder_dm_hours,
            "reminder_chat_enabled": self.reminder_chat_enabled,
            "playoff_stage_config": self.playoff_stage_config,
            "tours_enabled": self.tours_enabled,
            "total_tours": self.total_tours,
            "auto_next_tour": self.auto_next_tour,
            "group_display_name": self.group_display_name or None,
        }

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TournamentTemplate":
        data = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ─────────────────────────────────────────────────────────────────────────────
# Built-in templates
# ─────────────────────────────────────────────────────────────────────────────

BUILTIN_TEMPLATES: dict[str, TournamentTemplate] = {
    "league": TournamentTemplate(
        name="Лига (чемпионат)",
        description=(
            "Круговой турнир — все играют против всех в одной группе. "
            "Победитель определяется по таблице. Плей-офф нет. "
            "Матчи разбиты на туры (2 круга = 2×(N-1) туров)."
        ),
        template_type="league",
        groups_only=1,
        groups_count=1,
        group_matches_per_pair=2,  # double round-robin (home + away)
        playoff_third_place=0,
        draw_mode="random",
        tours_enabled=1,
        total_tours=0,  # auto = 2*(N-1)
        group_display_name="Лига",
    ),
    "league_single": TournamentTemplate(
        name="Лига (один круг)",
        description=(
            "Круговой турнир в один круг — каждый играет с каждым один раз. "
            "Победитель по таблице. Матчи разбиты на туры (N-1 туров)."
        ),
        template_type="league",
        groups_only=1,
        groups_count=1,
        group_matches_per_pair=1,
        playoff_third_place=0,
        draw_mode="random",
        tours_enabled=1,
        total_tours=0,  # auto = N-1
        group_display_name="Лига",
    ),
    "league_groups": TournamentTemplate(
        name="Лига с группами",
        description=(
            "Несколько групп, круговой турнир в каждой. "
            "Без плей-офф — лучшие в группах определяют итог."
        ),
        template_type="league",
        groups_only=1,
        groups_count=4,
        group_matches_per_pair=1,
        playoff_third_place=0,
        draw_mode="random",
    ),
    "cup": TournamentTemplate(
        name="Кубок (олимпийская система)",
        description=(
            "Одноматчевый нокаут-турнир. Проиграл — вылетел. "
            "Жеребьёвка может быть автоматической (по ELO), "
            "случайной или ручной."
        ),
        template_type="cup",
        bracket_only=1,
        groups_count=0,
        playoff_matches_per_pair=1,
        playoff_advance_mode="goals",
        playoff_third_place=1,
        draw_mode="auto",
    ),
    "cup_bo3": TournamentTemplate(
        name="Кубок (до 2 побед / bo3)",
        description=(
            "Нокаут-турнир: каждый раунд играется до 2 побед (best of 3). "
            "Проиграл серию — вылетел."
        ),
        template_type="cup",
        bracket_only=1,
        groups_count=0,
        playoff_matches_per_pair=3,
        playoff_advance_mode="wins",
        playoff_third_place=1,
        draw_mode="auto",
    ),
    "cup_2leg": TournamentTemplate(
        name="Кубок (2 матча, по сумме голов)",
        description=(
            "Нокаут-турнир: каждая пара играет 2 матча. "
            "Проходит тот, кто забил больше по сумме двух матчей."
        ),
        template_type="cup",
        bracket_only=1,
        groups_count=0,
        playoff_matches_per_pair=2,
        playoff_advance_mode="goals",
        playoff_third_place=1,
        draw_mode="auto",
    ),
    "groups_playoff": TournamentTemplate(
        name="Группы + Плей-офф",
        description=(
            "Классический формат: групповой этап (round-robin), затем "
            "плей-офф из лучших игроков каждой группы."
        ),
        template_type="groups_playoff",
        bracket_only=0,
        groups_only=0,
        groups_count=4,
        playoff_slots=2,
        group_matches_per_pair=1,
        playoff_matches_per_pair=1,
        playoff_third_place=1,
        draw_mode="auto",
    ),
    "champions_league": TournamentTemplate(
        name="Лига Чемпионов",
        description=(
            "Формат ЛЧ: 4 группы по 4, двойной круговой (дома и в гостях), "
            "2 лучших из каждой группы в плей-офф (2 матча, по голам)."
        ),
        template_type="groups_playoff",
        bracket_only=0,
        groups_only=0,
        groups_count=4,
        target_group_size=4,
        playoff_slots=2,
        group_matches_per_pair=2,
        playoff_matches_per_pair=2,
        playoff_advance_mode="goals",
        playoff_third_place=0,
        draw_mode="auto",
    ),
}


def get_builtin_template(key: str) -> TournamentTemplate | None:
    """Return a built-in template by key, or None."""
    return BUILTIN_TEMPLATES.get(key)


def list_builtin_templates() -> list[tuple[str, TournamentTemplate]]:
    """Return all built-in templates as (key, template) pairs."""
    return list(BUILTIN_TEMPLATES.items())


def list_templates_by_type(template_type: str) -> list[tuple[str, TournamentTemplate]]:
    """Filter built-in templates by type (league/cup/groups_playoff/custom)."""
    return [
        (k, t) for k, t in BUILTIN_TEMPLATES.items()
        if t.template_type == template_type
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Custom templates (DB-backed)
# ─────────────────────────────────────────────────────────────────────────────

def save_custom_template(
    name: str,
    created_by: int,
    template: TournamentTemplate,
) -> int:
    """Save a custom template to the database. Returns the template ID."""
    from database import get_conn
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO tournament_templates (name, created_by, config_json)
           VALUES (?, ?, ?)""",
        (name, created_by, template.to_json()),
    )
    conn.commit()
    tid = c.lastrowid
    conn.close()
    return tid


def get_custom_template(template_id: int) -> TournamentTemplate | None:
    """Load a custom template by ID."""
    from database import get_conn
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM tournament_templates WHERE id = ?", (template_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return TournamentTemplate.from_json(row["config_json"])


def list_custom_templates(created_by: int | None = None) -> list[dict]:
    """List custom templates, optionally filtered by creator."""
    from database import get_conn
    conn = get_conn()
    if created_by is not None:
        rows = conn.execute(
            "SELECT * FROM tournament_templates WHERE created_by = ? "
            "ORDER BY created_at DESC",
            (created_by,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tournament_templates ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_custom_template(template_id: int) -> bool:
    """Delete a custom template. Returns True if deleted."""
    from database import get_conn
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tournament_templates WHERE id = ?", (template_id,))
    conn.commit()
    deleted = (c.rowcount or 0) > 0
    conn.close()
    return deleted
