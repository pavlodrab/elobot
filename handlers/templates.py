"""Tournament template handlers — interactive tournament creation flow.

Provides:
- /new_tournament — interactive wizard with template selection
- /templates — list available templates
- /save_template — save current tournament config as a custom template
- /delete_template — remove a custom template
- Callback handlers for the inline template picker buttons
- /draw_manual — manual draw for cup tournaments
"""

from __future__ import annotations

import html
import json
import logging
import random

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

import database as db
from database import (
    create_tournament,
    get_active_tournament,
    get_tournament,
    get_tournament_players,
    update_tournament,
)
from handlers._helpers import _can_manage_tournament, _player_from_user
from handlers.common import (
    is_admin,
    mention,
    parse_tournament_type_arg,
    send,
    t_type_label,
)
from tournament_templates import (
    BUILTIN_TEMPLATES,
    TournamentTemplate,
    delete_custom_template,
    get_builtin_template,
    get_custom_template,
    list_builtin_templates,
    list_custom_templates,
    save_custom_template,
)

log = logging.getLogger(__name__)



# ─────────────────────────────────────────────────────────────────────────────
# /new_tournament — interactive creation wizard
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_new_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Start the interactive tournament creation wizard.

    Shows a menu of template categories, then templates within
    the chosen category, then asks for name and game type.
    """
    user = update.effective_user
    if not is_admin(user.id):
        await send(
            update,
            "❌ Создавать турниры могут только админы.",
        )
        return

    # Show category picker
    rows = [
        [
            InlineKeyboardButton(
                "🏅 Лига (чемпионат)", callback_data="tpl_cat:league"
            ),
        ],
        [
            InlineKeyboardButton(
                "🏆 Кубок (нокаут)", callback_data="tpl_cat:cup"
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Группы + Плей-офф", callback_data="tpl_cat:groups_playoff"
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ Свой шаблон", callback_data="tpl_cat:custom"
            ),
        ],
    ]
    # Check if user has custom templates
    customs = list_custom_templates(created_by=user.id)
    if customs:
        rows.append([
            InlineKeyboardButton(
                f"📁 Мои шаблоны ({len(customs)})",
                callback_data="tpl_cat:my_templates",
            ),
        ])

    await send(
        update,
        "🏟 <b>Создание турнира</b>\n\n"
        "Выбери тип турнира:",
        reply_markup=InlineKeyboardMarkup(rows),
    )



# ─────────────────────────────────────────────────────────────────────────────
# Callback: category selected → show templates in that category
# ─────────────────────────────────────────────────────────────────────────────

async def cb_template_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_cat:<category> callback."""
    query = update.callback_query
    await query.answer()
    data = query.data  # tpl_cat:<category>
    _, category = data.split(":", 1)

    if category == "my_templates":
        await _show_my_templates(query, update.effective_user.id)
        return

    if category == "custom":
        await _show_custom_wizard(query)
        return

    # Show built-in templates for this category
    templates = [
        (k, t) for k, t in BUILTIN_TEMPLATES.items()
        if t.template_type == category
    ]
    if not templates:
        await query.message.edit_text(
            "❌ Нет шаблонов для этой категории.",
            parse_mode="HTML",
        )
        return

    rows = []
    for key, tpl in templates:
        rows.append([
            InlineKeyboardButton(
                tpl.name, callback_data=f"tpl_pick:{key}"
            )
        ])
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="tpl_back_main")
    ])

    cat_names = {
        "league": "🏅 Лига",
        "cup": "🏆 Кубок",
        "groups_playoff": "📊 Группы + Плей-офф",
    }
    await query.message.edit_text(
        f"<b>{cat_names.get(category, category)}</b>\n\n"
        "Выбери шаблон:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )



async def _show_my_templates(query, user_id: int):
    """Show user's custom templates."""
    customs = list_custom_templates(created_by=user_id)
    if not customs:
        await query.message.edit_text(
            "📁 У тебя пока нет сохранённых шаблонов.\n"
            "Создай турнир через ⚙️ <b>Свой шаблон</b> и сохрани его.",
            parse_mode="HTML",
        )
        return

    rows = []
    for ct in customs[:10]:  # max 10 shown
        tpl = TournamentTemplate.from_json(ct["config_json"])
        rows.append([
            InlineKeyboardButton(
                f"{ct['name']}", callback_data=f"tpl_custom:{ct['id']}"
            )
        ])
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="tpl_back_main")
    ])
    await query.message.edit_text(
        "📁 <b>Мои шаблоны</b>\n\nВыбери шаблон для создания турнира:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_custom_wizard(query):
    """Show the custom template configuration options."""
    text = (
        "⚙️ <b>Свой шаблон</b>\n\n"
        "Выбери базовый формат, затем настрой параметры:\n\n"
        "• <b>Лига</b> — все vs все, таблица\n"
        "• <b>Кубок</b> — нокаут-бракет\n"
        "• <b>Группы+ПО</b> — группы → плей-офф"
    )
    rows = [
        [
            InlineKeyboardButton(
                "Лига", callback_data="tpl_cust_base:league"
            ),
            InlineKeyboardButton(
                "Кубок", callback_data="tpl_cust_base:cup"
            ),
            InlineKeyboardButton(
                "Группы+ПО", callback_data="tpl_cust_base:groups_playoff"
            ),
        ],
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="tpl_back_main")
        ],
    ]
    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )



# ─────────────────────────────────────────────────────────────────────────────
# Callback: template picked → show details + confirm
# ─────────────────────────────────────────────────────────────────────────────

async def cb_template_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_pick:<key> — show template details and confirm creation."""
    query = update.callback_query
    await query.answer()
    data = query.data  # tpl_pick:<key>
    _, key = data.split(":", 1)

    tpl = get_builtin_template(key)
    if not tpl:
        await query.message.edit_text("❌ Шаблон не найден.")
        return

    info = _format_template_info(tpl)
    rows = [
        [
            InlineKeyboardButton(
                "⚽ Создать (ВСА)", callback_data=f"tpl_create:{key}:vsa"
            ),
            InlineKeyboardButton(
                "⚽ Создать (РИ)", callback_data=f"tpl_create:{key}:ri"
            ),
        ],
    ]
    # Cup templates: add draw mode options
    if tpl.bracket_only:
        rows.append([
            InlineKeyboardButton(
                "🎲 Жребий: авто (ELO)",
                callback_data=f"tpl_draw:{key}:auto",
            ),
        ])
        rows.append([
            InlineKeyboardButton(
                "🔀 Жребий: случайный",
                callback_data=f"tpl_draw:{key}:random",
            ),
        ])
        rows.append([
            InlineKeyboardButton(
                "✋ Жребий: ручной",
                callback_data=f"tpl_draw:{key}:manual",
            ),
        ])
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="tpl_back_main")
    ])

    await query.message.edit_text(
        info,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_template_custom_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_custom:<id> — use a saved custom template."""
    query = update.callback_query
    await query.answer()
    data = query.data  # tpl_custom:<id>
    _, tid_str = data.split(":", 1)

    tpl = get_custom_template(int(tid_str))
    if not tpl:
        await query.message.edit_text("❌ Шаблон не найден или удалён.")
        return

    info = _format_template_info(tpl)
    rows = [
        [
            InlineKeyboardButton(
                "⚽ Создать (ВСА)",
                callback_data=f"tpl_cust_create:{tid_str}:vsa",
            ),
            InlineKeyboardButton(
                "⚽ Создать (РИ)",
                callback_data=f"tpl_cust_create:{tid_str}:ri",
            ),
        ],
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:my_templates")
        ],
    ]
    await query.message.edit_text(info, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(rows))



# ─────────────────────────────────────────────────────────────────────────────
# Callback: draw mode selected for cup
# ─────────────────────────────────────────────────────────────────────────────

async def cb_template_draw_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_draw:<key>:<mode> — set draw mode preference in user_data."""
    query = update.callback_query
    await query.answer()
    data = query.data  # tpl_draw:<key>:<mode>
    parts = data.split(":")
    key, mode = parts[1], parts[2]

    # Store preference in user_data for when they press "Create"
    ctx.user_data["tpl_draw_mode"] = mode
    mode_labels = {"auto": "авто (по ELO)", "random": "случайный", "manual": "ручной"}
    await query.answer(f"✅ Жребий: {mode_labels.get(mode, mode)}", show_alert=False)


# ─────────────────────────────────────────────────────────────────────────────
# Callback: create tournament from builtin template
# ─────────────────────────────────────────────────────────────────────────────

async def cb_template_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_create:<key>:<type> — actually create the tournament."""
    query = update.callback_query
    await query.answer()
    data = query.data  # tpl_create:<key>:<type>
    parts = data.split(":")
    key, t_type = parts[1], parts[2]

    user = update.effective_user
    if not is_admin(user.id):
        await query.message.edit_text("❌ Только админы могут создавать турниры.")
        return

    tpl = get_builtin_template(key)
    if not tpl:
        await query.message.edit_text("❌ Шаблон не найден.")
        return

    # Draw mode (user may have selected one)
    draw_mode = ctx.user_data.pop("tpl_draw_mode", tpl.draw_mode)

    # Ask for name first
    ctx.user_data["pending_tpl_create"] = {
        "source": "builtin",
        "key": key,
        "t_type": t_type,
        "draw_mode": draw_mode,
    }
    await query.message.edit_text(
        f"📝 Отправь <b>название турнира</b> одним сообщением.\n"
        f"Шаблон: <b>{html.escape(tpl.name)}</b>\n"
        f"Тип: <b>{t_type_label(t_type)}</b>\n\n"
        f"Можешь добавить правила после названия через символ <b>|</b>\n"
        f"Пример: <code>ЧМ-2026 | только ВСА, бо-3</code>\n\n"
        f"Или отправь <code>/skip</code> для автогенерации.",
        parse_mode="HTML",
    )



# ─────────────────────────────────────────────────────────────────────────────
# Callback: create tournament from custom template
# ─────────────────────────────────────────────────────────────────────────────

async def cb_template_custom_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_cust_create:<id>:<type>."""
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(":")
    tpl_id, t_type = int(parts[1]), parts[2]

    user = update.effective_user
    if not is_admin(user.id):
        await query.message.edit_text("❌ Только админы могут создавать турниры.")
        return

    tpl = get_custom_template(tpl_id)
    if not tpl:
        await query.message.edit_text("❌ Шаблон не найден или удалён.")
        return

    creator = _player_from_user(user)
    if not creator:
        await query.message.edit_text("❌ Сначала зарегистрируйся: /register")
        return

    from datetime import datetime
    name = f"{tpl.name} #{datetime.utcnow().strftime('%d%m%y')}"

    chat = update.effective_chat
    auto_bind = None
    if chat and chat.type in ("group", "supergroup", "channel"):
        auto_bind = chat.id

    tid = create_tournament(
        name,
        tournament_type=t_type,
        created_by=creator["id"],
        is_official=True,
        chat_id=auto_bind,
    )

    kwargs = tpl.to_tournament_kwargs()
    kwargs["draw_mode"] = tpl.draw_mode
    kwargs["template_id"] = tpl_id
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    update_tournament(tid, **kwargs)

    await query.message.edit_text(
        f"🏆 Турнир <b>{html.escape(name)}</b> создан!\n"
        f"📋 Шаблон: <b>{html.escape(tpl.name)}</b>\n"
        f"⚽ Тип: <b>{t_type_label(t_type)}</b>\n"
        f"ID: {tid}\n\n"
        f"Добавляй игроков: <code>/add_player @user1, @user2</code>\n"
        f"Запуск: <code>/start_tournament</code>",
        parse_mode="HTML",
    )



# ─────────────────────────────────────────────────────────────────────────────
# Callback: custom base type selected → configure params
# ─────────────────────────────────────────────────────────────────────────────

async def cb_custom_base(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_cust_base:<type> — start custom config wizard."""
    query = update.callback_query
    await query.answer()
    data = query.data
    _, base = data.split(":", 1)

    # Store in user_data
    ctx.user_data["tpl_custom_base"] = base
    ctx.user_data["tpl_custom_cfg"] = {}

    if base == "league":
        text = (
            "⚙️ <b>Настройка лиги</b>\n\n"
            "Выбери количество кругов:"
        )
        rows = [
            [
                InlineKeyboardButton("1 круг", callback_data="tpl_cfg:rounds:1"),
                InlineKeyboardButton("2 круга", callback_data="tpl_cfg:rounds:2"),
                InlineKeyboardButton("3 круга", callback_data="tpl_cfg:rounds:3"),
                InlineKeyboardButton("4 круга", callback_data="tpl_cfg:rounds:4"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")
            ],
        ]
    elif base == "cup":
        text = (
            "⚙️ <b>Настройка кубка</b>\n\n"
            "Формат матчей в раунде:"
        )
        rows = [
            [
                InlineKeyboardButton(
                    "1 матч", callback_data="tpl_cfg:legs:1"
                ),
                InlineKeyboardButton(
                    "2 матча (сумма голов)", callback_data="tpl_cfg:legs:2"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Bo3 (до 2 побед)", callback_data="tpl_cfg:legs:3"
                ),
                InlineKeyboardButton(
                    "Bo5 (до 3 побед)", callback_data="tpl_cfg:legs:5"
                ),
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")
            ],
        ]
    else:  # groups_playoff
        text = (
            "⚙️ <b>Настройка: Группы + Плей-офф</b>\n\n"
            "Сколько групп?"
        )
        rows = [
            [
                InlineKeyboardButton(str(n), callback_data=f"tpl_cfg:groups:{n}")
                for n in [2, 4, 6, 8]
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")
            ],
        ]

    await query.message.edit_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
    )



# ─────────────────────────────────────────────────────────────────────────────
# Callback: config param set in custom wizard
# ─────────────────────────────────────────────────────────────────────────────

async def cb_config_param(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_cfg:<param>:<value> — set a config parameter."""
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(":")
    param, value = parts[1], parts[2]

    cfg = ctx.user_data.get("tpl_custom_cfg", {})
    cfg[param] = value
    ctx.user_data["tpl_custom_cfg"] = cfg
    base = ctx.user_data.get("tpl_custom_base", "league")

    # Determine next step based on base type and what's configured
    if base == "league" and param == "rounds":
        # Ask about groups
        text = "Сколько групп? (1 = все в одной лиге)"
        rows = [
            [
                InlineKeyboardButton(str(n), callback_data=f"tpl_cfg:groups:{n}")
                for n in [1, 2, 4, 6]
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")],
        ]
        await query.message.edit_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if base == "league" and param == "groups":
        # Ask about tours
        text = "📅 Разбить матчи на туры?"
        rows = [
            [
                InlineKeyboardButton("❌ Нет", callback_data="tpl_cfg:tours:0"),
            ],
            [
                InlineKeyboardButton("авто", callback_data="tpl_cfg:tours:1"),
                InlineKeyboardButton("4 тура", callback_data="tpl_cfg:tours:4"),
                InlineKeyboardButton("6 туров", callback_data="tpl_cfg:tours:6"),
                InlineKeyboardButton("8 туров", callback_data="tpl_cfg:tours:8"),
            ],
            [
                InlineKeyboardButton("10 т.", callback_data="tpl_cfg:tours:10"),
                InlineKeyboardButton("14 т.", callback_data="tpl_cfg:tours:14"),
                InlineKeyboardButton("20 т.", callback_data="tpl_cfg:tours:20"),
                InlineKeyboardButton("30 т.", callback_data="tpl_cfg:tours:30"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")],
        ]
        await query.message.edit_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if base == "cup" and param == "legs":
        # Ask about draw mode
        text = "🎲 Способ жеребьёвки:"
        rows = [
            [InlineKeyboardButton(
                "Авто (по ELO)", callback_data="tpl_cfg:draw:auto"
            )],
            [InlineKeyboardButton(
                "Случайный жребий", callback_data="tpl_cfg:draw:random"
            )],
            [InlineKeyboardButton(
                "Ручной жребий", callback_data="tpl_cfg:draw:manual"
            )],
            [InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")],
        ]
        await query.message.edit_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if base == "cup" and param == "draw":
        # Ask about 3rd place match
        text = "Матч за 3-е место?"
        rows = [
            [
                InlineKeyboardButton("✅ Да", callback_data="tpl_cfg:third:1"),
                InlineKeyboardButton("❌ Нет", callback_data="tpl_cfg:third:0"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")],
        ]
        await query.message.edit_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if base == "groups_playoff" and param == "groups":
        # Ask playoff slots
        text = "Сколько игроков проходят из группы в плей-офф?"
        rows = [
            [
                InlineKeyboardButton(str(n), callback_data=f"tpl_cfg:slots:{n}")
                for n in [1, 2, 3, 4]
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")],
        ]
        await query.message.edit_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if base == "groups_playoff" and param == "slots":
        # Ask about group round-robin
        text = "Количество кругов в группе:"
        rows = [
            [
                InlineKeyboardButton("1 круг", callback_data="tpl_cfg:rounds:1"),
                InlineKeyboardButton("2 круга", callback_data="tpl_cfg:rounds:2"),
                InlineKeyboardButton("3 круга", callback_data="tpl_cfg:rounds:3"),
                InlineKeyboardButton("4 круга", callback_data="tpl_cfg:rounds:4"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")],
        ]
        await query.message.edit_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    # Final step: show summary and offer to create or save
    await _show_custom_summary(query, ctx)



async def _show_custom_summary(query, ctx: ContextTypes.DEFAULT_TYPE):
    """Show final summary of the custom template and offer create/save."""
    base = ctx.user_data.get("tpl_custom_base", "league")
    cfg = ctx.user_data.get("tpl_custom_cfg", {})

    tpl = _build_custom_template(base, cfg)
    info = _format_template_info(tpl)

    rows = [
        [
            InlineKeyboardButton(
                "⚽ Создать (ВСА)", callback_data="tpl_cust_final:vsa"
            ),
            InlineKeyboardButton(
                "⚽ Создать (РИ)", callback_data="tpl_cust_final:ri"
            ),
        ],
        [
            InlineKeyboardButton(
                "💾 Сохранить как шаблон", callback_data="tpl_cust_save"
            ),
        ],
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="tpl_cat:custom")
        ],
    ]
    await query.message.edit_text(
        f"⚙️ <b>Свой шаблон — итог</b>\n\n{info}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _build_custom_template(base: str, cfg: dict) -> TournamentTemplate:
    """Build a TournamentTemplate from the wizard config dict."""
    rounds = int(cfg.get("rounds", 1))
    groups = int(cfg.get("groups", 1))
    legs = int(cfg.get("legs", 1))
    draw = cfg.get("draw", "auto")
    third = int(cfg.get("third", 1))
    slots = int(cfg.get("slots", 2))
    tours = int(cfg.get("tours", 0))

    if base == "league":
        tours_enabled = 1 if tours > 0 else 0
        total_tours = tours
        return TournamentTemplate(
            name="Свой шаблон (лига)",
            template_type="league",
            groups_only=1,
            groups_count=groups,
            group_matches_per_pair=rounds,
            playoff_third_place=0,
            draw_mode="random",
            tours_enabled=tours_enabled,
            total_tours=total_tours,
            group_display_name="Лига",
        )
    elif base == "cup":
        mode = "wins" if legs >= 3 else "goals"
        return TournamentTemplate(
            name="Свой шаблон (кубок)",
            template_type="cup",
            bracket_only=1,
            groups_count=0,
            playoff_matches_per_pair=legs,
            playoff_advance_mode=mode,
            playoff_third_place=third,
            draw_mode=draw,
        )
    else:  # groups_playoff
        return TournamentTemplate(
            name="Свой шаблон (группы+ПО)",
            template_type="groups_playoff",
            bracket_only=0,
            groups_only=0,
            groups_count=groups,
            playoff_slots=slots,
            group_matches_per_pair=rounds,
            playoff_matches_per_pair=1,
            playoff_third_place=1,
            draw_mode="auto",
        )



# ─────────────────────────────────────────────────────────────────────────────
# Callback: final create from custom wizard
# ─────────────────────────────────────────────────────────────────────────────

async def cb_custom_final_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_cust_final:<type> — create tournament from wizard config."""
    query = update.callback_query
    await query.answer()
    data = query.data
    _, t_type = data.split(":", 1)

    user = update.effective_user
    if not is_admin(user.id):
        await query.message.edit_text("❌ Только админы.")
        return

    # Save creation config, ask for name
    ctx.user_data["pending_tpl_create"] = {
        "source": "custom",
        "t_type": t_type,
    }
    await query.message.edit_text(
        "📝 Отправь <b>название турнира</b> одним сообщением.\n\n"
        "Можешь добавить правила после названия через символ <b>|</b>\n"
        "Пример: <code>ЧМ-2026 | только ВСА, бо-3</code>\n\n"
        "Или отправь <code>/skip</code> для автогенерации.",
        parse_mode="HTML",
    )


async def cb_custom_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_cust_save — save the custom config as a template."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    base = ctx.user_data.get("tpl_custom_base", "league")
    cfg = ctx.user_data.get("tpl_custom_cfg", {})
    tpl = _build_custom_template(base, cfg)

    from datetime import datetime
    tpl_name = f"Мой шаблон ({base}) {datetime.utcnow().strftime('%d.%m.%y')}"
    tpl.name = tpl_name

    tpl_id = save_custom_template(
        name=tpl_name,
        created_by=user.id,
        template=tpl,
    )

    await query.message.edit_text(
        f"💾 Шаблон <b>{html.escape(tpl_name)}</b> сохранён (ID: {tpl_id})!\n\n"
        f"Используй его через /new_tournament → Мои шаблоны.",
        parse_mode="HTML",
    )



# ─────────────────────────────────────────────────────────────────────────────
# Callback: back to main menu
# ─────────────────────────────────────────────────────────────────────────────

async def cb_back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tpl_back_main — return to category picker."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    rows = [
        [InlineKeyboardButton("🏅 Лига (чемпионат)", callback_data="tpl_cat:league")],
        [InlineKeyboardButton("🏆 Кубок (нокаут)", callback_data="tpl_cat:cup")],
        [InlineKeyboardButton("📊 Группы + Плей-офф", callback_data="tpl_cat:groups_playoff")],
        [InlineKeyboardButton("⚙️ Свой шаблон", callback_data="tpl_cat:custom")],
    ]
    customs = list_custom_templates(created_by=user.id)
    if customs:
        rows.append([
            InlineKeyboardButton(
                f"📁 Мои шаблоны ({len(customs)})",
                callback_data="tpl_cat:my_templates",
            ),
        ])

    await query.message.edit_text(
        "🏟 <b>Создание турнира</b>\n\nВыбери тип турнира:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )



# ─────────────────────────────────────────────────────────────────────────────
# /templates — list all available templates
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_templates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all available templates (builtin + custom)."""
    lines = ["📋 <b>Доступные шаблоны турниров</b>\n"]

    lines.append("<b>🏅 Лига:</b>")
    for key, tpl in BUILTIN_TEMPLATES.items():
        if tpl.template_type == "league":
            lines.append(f"  • <code>{key}</code> — {tpl.name}")
    lines.append("")

    lines.append("<b>🏆 Кубок:</b>")
    for key, tpl in BUILTIN_TEMPLATES.items():
        if tpl.template_type == "cup":
            lines.append(f"  • <code>{key}</code> — {tpl.name}")
    lines.append("")

    lines.append("<b>📊 Группы + Плей-офф:</b>")
    for key, tpl in BUILTIN_TEMPLATES.items():
        if tpl.template_type == "groups_playoff":
            lines.append(f"  • <code>{key}</code> — {tpl.name}")
    lines.append("")

    # Custom templates
    user = update.effective_user
    customs = list_custom_templates(created_by=user.id) if user else []
    if customs:
        lines.append("<b>📁 Мои шаблоны:</b>")
        for ct in customs:
            lines.append(f"  • ID {ct['id']} — {ct['name']}")
        lines.append("")

    lines.append(
        "Используй /new_tournament для интерактивного создания "
        "или /create_tournament для быстрого."
    )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /save_template <tournament_id> <name> — save existing tournament as template
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_save_template(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Save an existing tournament's settings as a reusable custom template."""
    user = update.effective_user
    if not is_admin(user.id):
        await send(update, "❌ Только для админов.")
        return

    if not ctx.args or len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/save_template &lt;ID турнира&gt; &lt;Название&gt;</code>",
        )
        return

    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ Первый аргумент — ID турнира (число).")
        return

    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир ID {tid} не найден.")
        return

    tpl_name = " ".join(ctx.args[1:])

    # Build template from tournament settings
    tpl = TournamentTemplate(
        name=tpl_name,
        template_type=_detect_template_type(t),
        tournament_type=t.get("tournament_type", "vsa"),
        bracket_only=int(t.get("bracket_only") or 0),
        groups_only=int(t.get("groups_only") or 0),
        groups_count=int(t.get("groups_count") or 1),
        target_group_size=t.get("target_group_size"),
        playoff_slots=int(t.get("playoff_slots") or 2),
        group_matches_per_pair=int(t.get("group_matches_per_pair") or 1),
        playoff_matches_per_pair=int(t.get("playoff_matches_per_pair") or 1),
        series_length=int(t.get("series_length") or 0),
        playoff_advance_mode=t.get("playoff_advance_mode") or "goals",
        playoff_third_place=int(t.get("playoff_third_place") or 1),
        draw_mode=t.get("draw_mode") or "auto",
        auto_tech_loss_enabled=int(t.get("auto_tech_loss_enabled") or 0),
        auto_tech_loss_score=t.get("auto_tech_loss_score") or "0:3",
        auto_confirm=int(t.get("auto_confirm") or 0),
        open_signup=int(t.get("open_signup") or 1),
        reminder_dm_hours=int(t.get("reminder_dm_hours") or 12),
        reminder_chat_enabled=int(t.get("reminder_chat_enabled") or 0),
        playoff_stage_config=t.get("playoff_stage_config") or "{}",
    )

    tpl_id = save_custom_template(
        name=tpl_name,
        created_by=user.id,
        template=tpl,
    )
    await send(
        update,
        f"💾 Шаблон <b>{html.escape(tpl_name)}</b> сохранён (ID: {tpl_id})!\n"
        f"Используй через /new_tournament → Мои шаблоны.",
    )



# ─────────────────────────────────────────────────────────────────────────────
# /delete_template <id>
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_delete_template(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Delete a custom template by ID."""
    user = update.effective_user
    if not ctx.args:
        await send(update, "Использование: <code>/delete_template &lt;ID&gt;</code>")
        return

    try:
        tpl_id = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return

    # Check ownership
    customs = list_custom_templates(created_by=user.id)
    owns = any(c["id"] == tpl_id for c in customs)
    if not owns and not is_admin(user.id):
        await send(update, "❌ Это не твой шаблон.")
        return

    if delete_custom_template(tpl_id):
        await send(update, f"🗑 Шаблон ID {tpl_id} удалён.")
    else:
        await send(update, f"❌ Шаблон ID {tpl_id} не найден.")



# ─────────────────────────────────────────────────────────────────────────────
# /draw_manual — manual bracket draw for cup tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_draw_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Start manual draw process for a cup tournament.

    Usage: /draw_manual [tournament_id]

    Shows the list of players and lets the admin pick pairs one by one
    via inline buttons.
    """
    user = update.effective_user
    if not is_admin(user.id):
        await send(update, "❌ Только для админов.")
        return

    from handlers._helpers import _resolve_tournament_from_args
    t, err = _resolve_tournament_from_args(update, ctx)
    if not t:
        await send(update, err or "❌ Турнир не найден.")
        return

    if not _can_manage_tournament(user.id, t):
        await send(update, "❌ Только создатель или админ.")
        return

    if not int(t.get("bracket_only") or 0):
        await send(
            update,
            "❌ Ручной жребий доступен только для кубковых турниров "
            "(bracket_only). Для групповых используй /redraw_groups.",
        )
        return

    players = get_tournament_players(t["id"])
    if len(players) < 2:
        await send(update, "❌ Нужно минимум 2 игрока для жеребьёвки.")
        return

    # Check if bracket already exists
    from tournament import PLAYOFF_STAGES
    for s in PLAYOFF_STAGES:
        existing = db.get_tournament_matches(t["id"], stage=s)
        if existing:
            await send(
                update,
                "❌ Бракет уже создан. Используй /reset_bracket для сброса "
                "(если нужно перерисовать).",
            )
            return

    # Initialize manual draw state
    player_list = []
    for r in players:
        p = db.get_player_by_id(r["player_id"])
        if p:
            player_list.append({
                "player_id": r["player_id"],
                "username": p.get("username", f"id{r['player_id']}"),
            })

    ctx.user_data["manual_draw"] = {
        "tournament_id": t["id"],
        "available": player_list,
        "pairs": [],
        "picking_first": None,  # player being paired
    }

    await _show_draw_picker(update, ctx)


async def _show_draw_picker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show available players for manual draw pairing."""
    state = ctx.user_data.get("manual_draw")
    if not state:
        return

    available = state["available"]
    pairs = state["pairs"]
    picking = state.get("picking_first")
    tid = state["tournament_id"]

    if len(available) == 0:
        # All paired — finalize
        await _finalize_manual_draw(update, ctx)
        return

    if len(available) == 1 and picking is None:
        # Odd player — gets a bye
        bye_player = available[0]
        pairs.append((bye_player, None))
        state["available"] = []
        await _finalize_manual_draw(update, ctx)
        return

    lines = [f"✋ <b>Ручная жеребьёвка</b> (турнир ID {tid})\n"]
    if pairs:
        lines.append("<b>Составленные пары:</b>")
        for i, (a, b) in enumerate(pairs, 1):
            b_name = mention(b["username"]) if b else "BYE"
            lines.append(f"  {i}. {mention(a['username'])} vs {b_name}")
        lines.append("")

    if picking:
        lines.append(
            f"Выбери соперника для <b>{mention(picking['username'])}</b>:"
        )
        rows = []
        for p in available:
            if p["player_id"] != picking["player_id"]:
                rows.append([
                    InlineKeyboardButton(
                        f"@{p['username']}",
                        callback_data=f"mdraw_pair:{p['player_id']}",
                    )
                ])
        rows.append([
            InlineKeyboardButton("❌ Отмена жеребьёвки", callback_data="mdraw_cancel")
        ])
    else:
        lines.append(f"Осталось: <b>{len(available)}</b> игроков")
        lines.append("Выбери первого игрока пары:")
        rows = []
        for p in available:
            rows.append([
                InlineKeyboardButton(
                    f"@{p['username']}",
                    callback_data=f"mdraw_first:{p['player_id']}",
                )
            ])
        rows.append([
            InlineKeyboardButton(
                "🔀 Авто-добить оставшихся", callback_data="mdraw_auto_rest"
            )
        ])
        rows.append([
            InlineKeyboardButton("❌ Отмена", callback_data="mdraw_cancel")
        ])

    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(rows)

    msg = update.callback_query.message if update.callback_query else update.effective_message
    if update.callback_query:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=markup)



async def cb_manual_draw_first(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle mdraw_first:<player_id> — select first player of a pair."""
    query = update.callback_query
    await query.answer()
    data = query.data
    _, pid_str = data.split(":", 1)
    pid = int(pid_str)

    state = ctx.user_data.get("manual_draw")
    if not state:
        await query.message.edit_text("❌ Сессия жеребьёвки истекла. Начни /draw_manual заново.")
        return

    # Find player in available
    player = None
    for p in state["available"]:
        if p["player_id"] == pid:
            player = p
            break
    if not player:
        await query.answer("❌ Игрок уже спарен.", show_alert=True)
        return

    state["picking_first"] = player
    await _show_draw_picker(update, ctx)


async def cb_manual_draw_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle mdraw_pair:<player_id> — pair the selected opponent."""
    query = update.callback_query
    await query.answer()
    data = query.data
    _, pid_str = data.split(":", 1)
    pid = int(pid_str)

    state = ctx.user_data.get("manual_draw")
    if not state or not state.get("picking_first"):
        await query.message.edit_text("❌ Сессия жеребьёвки истекла.")
        return

    first = state["picking_first"]
    second = None
    for p in state["available"]:
        if p["player_id"] == pid:
            second = p
            break
    if not second:
        await query.answer("❌ Игрок уже спарен.", show_alert=True)
        return

    # Add pair and remove from available
    state["pairs"].append((first, second))
    state["available"] = [
        p for p in state["available"]
        if p["player_id"] not in (first["player_id"], second["player_id"])
    ]
    state["picking_first"] = None

    await _show_draw_picker(update, ctx)


async def cb_manual_draw_auto_rest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle mdraw_auto_rest — randomly pair remaining players."""
    query = update.callback_query
    await query.answer()

    state = ctx.user_data.get("manual_draw")
    if not state:
        await query.message.edit_text("❌ Сессия истекла.")
        return

    available = state["available"]
    random.shuffle(available)

    while len(available) >= 2:
        a = available.pop(0)
        b = available.pop(0)
        state["pairs"].append((a, b))

    if available:
        # Odd player — bye
        state["pairs"].append((available.pop(0), None))

    state["available"] = []
    await _finalize_manual_draw(update, ctx)


async def cb_manual_draw_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle mdraw_cancel — cancel the manual draw session."""
    query = update.callback_query
    await query.answer()
    ctx.user_data.pop("manual_draw", None)
    await query.message.edit_text("❌ Жеребьёвка отменена.")



async def _finalize_manual_draw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Create the bracket matches from the manual draw pairs."""
    from datetime import datetime, timedelta
    from database import create_match, update_match
    from tournament import _next_pow2, _bracket_first_stage, MATCH_DEADLINE_HOURS

    state = ctx.user_data.get("manual_draw")
    if not state:
        return

    tid = state["tournament_id"]
    pairs = state["pairs"]
    t = get_tournament(tid)
    if not t:
        return

    legs = max(1, int(t.get("playoff_matches_per_pair") or 1))
    deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    dl_str = deadline.strftime("%Y-%m-%d %H:%M:%S")

    # Determine bracket size and stage
    n_players = sum(1 for a, b in pairs if b) * 2 + sum(1 for a, b in pairs if not b)
    bracket_size = _next_pow2(n_players)
    stage = _bracket_first_stage(bracket_size)

    created = []
    for a, b in pairs:
        if b:
            # Real match
            for leg in range(1, legs + 1):
                if leg % 2 == 1:
                    p1, p2 = a, b
                else:
                    p1, p2 = b, a
                mid = create_match(
                    tid, p1["player_id"], p2["player_id"],
                    stage=stage, deadline=dl_str, leg=leg,
                )
                created.append({
                    "stage": stage,
                    "player1": p1["username"],
                    "player2": p2["username"],
                    "match_id": mid,
                    "leg": leg,
                    "bye": False,
                })
        else:
            # Bye
            mid = create_match(
                tid, a["player_id"], a["player_id"],
                stage=stage, deadline=dl_str, leg=1,
            )
            update_match(mid, score1=1, score2=0, status="confirmed", reported_by=None)
            created.append({
                "stage": stage,
                "player1": a["username"],
                "player2": "BYE",
                "match_id": mid,
                "leg": 1,
                "bye": True,
            })

    update_tournament(tid, playoff_started=1, stage="playoff")

    # Clean up state
    ctx.user_data.pop("manual_draw", None)

    # Show result
    lines = [f"✅ <b>Жеребьёвка завершена!</b> (турнир ID {tid})\n"]
    lines.append(f"Раунд: <b>{stage}</b>, матчей: <b>{len(pairs)}</b>\n")
    for i, (a, b) in enumerate(pairs, 1):
        b_name = f"@{b['username']}" if b else "BYE"
        lines.append(f"  {i}. @{a['username']} vs {b_name}")

    lines.append(f"\nПосмотреть бракет: <code>/playoff {tid}</code>")
    text = "\n".join(lines)

    msg = update.callback_query.message if update.callback_query else update.effective_message
    if update.callback_query:
        await msg.edit_text(text, parse_mode="HTML")
    else:
        await msg.reply_text(text, parse_mode="HTML")



# ─────────────────────────────────────────────────────────────────────────────
# /draw_random — random bracket draw for cup tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_draw_random(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Create a randomly-drawn bracket (no ELO seeding).

    Usage: /draw_random [tournament_id]
    """
    user = update.effective_user
    if not is_admin(user.id):
        await send(update, "❌ Только для админов.")
        return

    from handlers._helpers import _resolve_tournament_from_args
    t, err = _resolve_tournament_from_args(update, ctx)
    if not t:
        await send(update, err or "❌ Турнир не найден.")
        return

    if not _can_manage_tournament(user.id, t):
        await send(update, "❌ Только создатель или админ.")
        return

    if not int(t.get("bracket_only") or 0):
        await send(update, "❌ Случайный жребий — только для кубковых турниров (bracket_only).")
        return

    players = get_tournament_players(t["id"])
    if len(players) < 2:
        await send(update, "❌ Нужно минимум 2 игрока.")
        return

    # Check existing bracket
    from tournament import PLAYOFF_STAGES
    for s in PLAYOFF_STAGES:
        if db.get_tournament_matches(t["id"], stage=s):
            await send(update, "❌ Бракет уже создан.")
            return

    # Build random bracket
    from datetime import datetime, timedelta
    from tournament import (
        _next_pow2, _bracket_first_stage, _bracket_seed_order,
        MATCH_DEADLINE_HOURS,
    )

    player_list = []
    for r in players:
        p = db.get_player_by_id(r["player_id"])
        if p:
            player_list.append({
                "player_id": r["player_id"],
                "username": p.get("username", f"id{r['player_id']}"),
            })

    random.shuffle(player_list)

    n = len(player_list)
    bracket_size = _next_pow2(n)
    stage = _bracket_first_stage(bracket_size)
    legs = max(1, int(t.get("playoff_matches_per_pair") or 1))
    deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    dl_str = deadline.strftime("%Y-%m-%d %H:%M:%S")

    # Place players into bracket slots using bracket_seed_order
    # but since we already shuffled, just assign sequentially
    seed_order = _bracket_seed_order(bracket_size)
    seed_to_player = {}
    for i, p in enumerate(player_list):
        seed_to_player[i + 1] = p

    created = []
    pairs_display = []
    for i in range(0, bracket_size, 2):
        sa, sb = seed_order[i], seed_order[i + 1]
        pa = seed_to_player.get(sa)
        pb = seed_to_player.get(sb)

        if pa and pb:
            for leg in range(1, legs + 1):
                if leg % 2 == 1:
                    a, b = pa, pb
                else:
                    a, b = pb, pa
                mid = db.create_match(
                    t["id"], a["player_id"], b["player_id"],
                    stage=stage, deadline=dl_str, leg=leg,
                )
                created.append(mid)
            pairs_display.append(f"@{pa['username']} vs @{pb['username']}")
        elif pa or pb:
            byed = pa or pb
            mid = db.create_match(
                t["id"], byed["player_id"], byed["player_id"],
                stage=stage, deadline=dl_str, leg=1,
            )
            db.update_match(mid, score1=1, score2=0, status="confirmed", reported_by=None)
            created.append(mid)
            pairs_display.append(f"@{byed['username']} vs BYE")

    update_tournament(t["id"], playoff_started=1, stage="playoff")

    lines = [f"🔀 <b>Случайная жеребьёвка завершена!</b>\n"]
    lines.append(f"Турнир: <b>{html.escape(t['name'])}</b> (ID {t['id']})")
    lines.append(f"Раунд: <b>{stage}</b>\n")
    for i, pair_str in enumerate(pairs_display, 1):
        lines.append(f"  {i}. {pair_str}")
    lines.append(f"\nПосмотреть бракет: <code>/playoff {t['id']}</code>")

    await send(update, "\n".join(lines))



# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

def _format_template_info(tpl: TournamentTemplate) -> str:
    """Format a template's settings into a readable summary."""
    lines = [f"<b>{html.escape(tpl.name)}</b>"]
    if tpl.description:
        lines.append(f"<i>{html.escape(tpl.description)}</i>")
    lines.append("")

    # Structure
    if tpl.bracket_only:
        lines.append("🏗 Формат: Нокаут (плей-офф)")
    elif tpl.groups_only:
        if tpl.groups_count == 1:
            lines.append("🏗 Формат: Лига (все vs все)")
        else:
            lines.append(f"🏗 Формат: Группы ({tpl.groups_count} шт.), без плей-офф")
    else:
        lines.append(f"🏗 Формат: {tpl.groups_count} групп → плей-офф")
        lines.append(f"  • Проходят из группы: {tpl.playoff_slots}")

    # Matches
    if not tpl.bracket_only:
        rr = {
            1: "одинарный",
            2: "двойной",
            3: "тройной",
            4: "4 круга",
        }.get(tpl.group_matches_per_pair, f"{tpl.group_matches_per_pair} кругов")
        lines.append(f"🔄 Круг в группе: {rr} ({tpl.group_matches_per_pair}×)")

    if tpl.bracket_only or not tpl.groups_only:
        if tpl.playoff_matches_per_pair == 1:
            lines.append("⚔️ Плей-офф: 1 матч")
        elif tpl.playoff_matches_per_pair == 2:
            lines.append("⚔️ Плей-офф: 2 матча (по сумме голов)")
        else:
            lines.append(
                f"⚔️ Плей-офф: до {(tpl.playoff_matches_per_pair + 1) // 2} "
                f"побед (bo{tpl.playoff_matches_per_pair})"
            )
        mode_lbl = "по голам" if tpl.playoff_advance_mode == "goals" else "по победам"
        lines.append(f"  • Определение победителя: {mode_lbl}")

    # Draw mode
    if tpl.bracket_only:
        lines.append(f"🎲 Жребий: {_draw_mode_label(tpl.draw_mode)}")

    # Third place
    if tpl.bracket_only or not tpl.groups_only:
        third = "да" if tpl.playoff_third_place else "нет"
        lines.append(f"🥉 Матч за 3-е место: {third}")

    # Tech loss
    if tpl.auto_tech_loss_enabled:
        lines.append(f"⏰ Авто-техпоражение: {tpl.auto_tech_loss_score}")

    # Tours
    if tpl.tours_enabled:
        total = str(tpl.total_tours) if tpl.total_tours else "авто"
        auto = "вкл" if tpl.auto_next_tour else "выкл"
        lines.append(f"📅 Туры: {total}, авто-переход: {auto}")

    return "\n".join(lines)


def _draw_mode_label(mode: str) -> str:
    """Human-readable draw mode label."""
    return {
        "auto": "авто (по ELO)",
        "random": "случайный",
        "manual": "ручной",
    }.get(mode, mode)


def _detect_template_type(t: dict) -> str:
    """Detect template type from tournament settings."""
    if int(t.get("bracket_only") or 0):
        return "cup"
    if int(t.get("groups_only") or 0):
        return "league"
    return "groups_playoff"


async def handle_pending_tpl_create_text(update, ctx, text: str) -> bool:
    """Handle text input for pending tournament name/desc from wizard.

    Returns True if consumed, False otherwise.
    """
    pending = ctx.user_data.pop("pending_tpl_create", None)
    if not pending:
        return False

    user = update.effective_user
    creator = _player_from_user(user)
    if not creator:
        await send(update, "❌ Сначала зарегистрируйся: /register")
        return True

    # Parse name and description
    name = text.strip()
    description = ""
    if "|" in name:
        parts = name.split("|", 1)
        name = parts[0].strip()
        description = parts[1].strip()

    if not name:
        from datetime import datetime
        name = f"Турнир #{datetime.utcnow().strftime('%d%m%y')}"

    t_type = pending.get("t_type", "vsa")

    chat = update.effective_chat
    auto_bind = None
    if chat and chat.type in ("group", "supergroup", "channel"):
        auto_bind = chat.id

    tid = create_tournament(
        name,
        tournament_type=t_type,
        created_by=creator["id"],
        is_official=True,
        chat_id=auto_bind,
    )

    if pending["source"] == "builtin":
        key = pending["key"]
        tpl = get_builtin_template(key)
        if tpl:
            kwargs = tpl.to_tournament_kwargs()
            kwargs["draw_mode"] = pending.get("draw_mode", tpl.draw_mode)
            if description:
                kwargs["description"] = description
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            update_tournament(tid, **kwargs)
            tours_line = ""
            if tpl.tours_enabled:
                total_str = str(tpl.total_tours) if tpl.total_tours else "авто"
                tours_line = f"\n📅 Туры: <b>{total_str}</b> (старт по /next_tour или авто)"
            await send(
                update,
                f"🏆 Турнир <b>{html.escape(name)}</b> создан!\n"
                f"📋 Шаблон: <b>{html.escape(tpl.name)}</b>\n"
                f"⚽ Тип: <b>{t_type_label(t_type)}</b>\n"
                f"{'📝 Правила: ' + html.escape(description) if description else ''}{tours_line}\n"
                f"ID: {tid}\n\n"
                f"Добавляй игроков: <code>/add_player @user1, @user2</code>\n"
                f"Запуск: <code>/start_tournament</code>",
            )
            return True
    elif pending["source"] == "custom":
        base = ctx.user_data.pop("tpl_custom_base", "league")
        cfg = ctx.user_data.pop("tpl_custom_cfg", {})
        tpl = _build_custom_template(base, cfg)
        kwargs = tpl.to_tournament_kwargs()
        kwargs["draw_mode"] = tpl.draw_mode
        if description:
            kwargs["description"] = description
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        update_tournament(tid, **kwargs)
        await send(
            update,
            f"🏆 Турнир <b>{html.escape(name)}</b> создан!\n"
            f"⚽ Тип: <b>{t_type_label(t_type)}</b>\n"
            f"{'📝 Правила: ' + html.escape(description) if description else ''}\n"
            f"ID: {tid}\n\n"
            f"Добавляй игроков: <code>/add_player @user1, @user2</code>\n"
            f"Запуск: <code>/start_tournament</code>",
        )
        return True

    # Fallback: just create with name only
    await send(
        update,
        f"🏆 Турнир <b>{html.escape(name)}</b> создан!\n"
        f"ID: {tid}\n\n"
        f"Добавляй игроков: <code>/add_player @user1, @user2</code>",
    )
    return True
