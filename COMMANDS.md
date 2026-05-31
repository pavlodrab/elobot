# FC League Bot — список всех команд

Все команды бота (84 уникальные функции, 201 алиас вместе со всеми вариантами написания).
Помечено: 👤 — игрок, 👮 — только админ (бот-админ или админ конкретного турнира).

---

## 👤 Старт / меню / профиль

| Команда | Что делает |
|---|---|
| `/start`, `/elodrak` | Старт бота, главное меню (в группах удобнее `/elodrak`). |
| `/help` | Справка для игроков. |
| `/admincmd`, `/admin_cmd`, `/adminhelp`, `/admin_help` | Справка для админов. |
| `/myid`, `/id`, `/whoami` | Показать `user_id` и `chat_id` (для `ADMIN_IDS`, `bind_tournament`). |
| `/keyboard`, `/kb`, `/menu_toggle`, `/toggle_menu` | Переключить нижнюю панель меню. |
| `/hide_keyboard`, `/hidekeyboard`, `/hide_menu`, `/hidemenu` | Скрыть нижнюю панель. |
| `/show_keyboard`, `/showkeyboard`, `/show_menu`, `/showmenu` | Показать нижнюю панель. |
| `/register` | Зарегистрироваться в лиге (старт ELO: 0). |
| `/setnick`, `/set_nick` `<InGameNickname>` | Указать свой ник в игре (нужен для авто-OCR скринов). |
| `/profile [@username]` | Профиль игрока. |
| `/matches` | Твои последние матчи. |
| `/my_deadlines`, `/mydeadlines`, `/deadlines` | Дедлайны твоих pending-матчей. |
| `/h2h`, `/vs <@user>` | История личных встреч с игроком. |
| `/feedback <текст>` | Отправить фидбэк / баг админам. |
| `/cancel` | Отменить активный визард / пошаговый ввод. |

---

## 👤 Турниры (просмотр)

| Команда | Что делает |
|---|---|
| `/tournaments` | Список активных турниров. |
| `/list_players`, `/listplayers [ID]` | Состав турнира (без ID — текущий чат / активный). |
| `/table`, `/standings [ID|вса|ри] [all|split|text|<группа>]` | Турнирная таблица (PNG / inline-селектор / `text` — чисто-текстовый вывод). |
| `/table_text`, `/standings_text [ID|вса|ри]` | Тот же `/table`, но всегда в текстовом виде (без PNG). |
| `/playoff`, `/bracket [ID|вса|ри] [text]` | Сетка плей-офф (PNG; `text` — без рендера). |
| `/playoff_text`, `/bracket_text [ID|вса|ри]` | Сетка плей-офф в текстовом виде. |
| `/playoff_preview`, `/playoffpreview`, `/preview_playoff [ID]` | Прогноз плей-офф по текущей таблице. |
| `/leaderboard [ID|вса|ри]` | Лидерборд конкретного турнира. |
| `/top` | Общий ELO-рейтинг (только официальные турниры). |
| `/top_vsa` | Топ ELO только по матчам ВСА. |
| `/top_ri` | Топ ELO только по матчам РИ. |
| `/top_scorers`, `/topscorers`, `/scorers`, `/bombardiry [all|<tid>]` | Бомбардиры. |
| `/tablebomb`, `/table_bomb`, `/bomb`, `/bombardiry_t`, `/bombs [ID|вса|ри]` | Таблица бомбардиров одного турнира. |

---

## 👤 Результаты матчей

| Команда | Что делает |
|---|---|
| 📸 *Фото скрина* | Авто-OCR в чате, привязанном к турниру (или с подписью `#турнир ID`). Сразу уходит на проверку админу. |
| `/report 3:2 @opponent [вса|ри]` | Сообщить результат вручную. |
| `/confirm` | Подтвердить результат, который выставил соперник. |
| `/dispute` | Оспорить выставленный соперником счёт. |

> ℹ️ После согласия соперника матч уходит **админу в личку с кнопками `[✅ Засчитать] [❌ Отклонить]`**.
> Если результат был засабмичен скрином — бот **прикладывает ту же картинку прямо в DM админу**, чтобы сверить счёт глазами.

---

## 👮 Управление турниром (создатель / админ)

| Команда | Что делает |
|---|---|
| `/create_tournament` | Визард создания турнира. |
| `/add_player @u1[, @u2, …]` | Добавить игроков **до старта** турнира. |
| `/replace_player`, `/replaceplayer @old @new [ID]` | Заменить игрока, не теряя расписание. |
| `/start_tournament` | Жеребьёвка + старт. Идемпотентна. |
| `/redraw_groups`, `/redrawgroups <ID>` | Перетряхнуть жеребьёвку (только до 1-го подтверждённого матча). |
| `/set_group`, `/setgroup <ID> @user <группа>` | Назначить игрока в группу руками. |
| `/clear_groups`, `/cleargroups <ID>` | Очистить распределение по группам. |
| `/start_playoff [ID|вса|ри]` | Запустить плей-офф. Идемпотентна. |
| `/redraw_playoff`, `/redrawplayoff [ID]` | Пересеять сетку плей-офф «крест по группам» (A1×B4, B2×A3, B1×A4, A2×B3). Только если ни один реальный матч ещё не подтверждён. |
| `/advance`, `/advance_playoff [ID]` | Вручную сдвинуть плей-офф / следующий раунд. |
| `/finish_tournament`, `/finishtournament`, `/end_tournament`, `/close_tournament [ID]` | Завершить турнир. |
| `/simulate`, `/autosim [ID]` | Авто-симуляция оставшихся матчей с ELO-весами. |
| `/bind_tournament <ID>` | Привязать чат / группу к турниру. |
| `/unbind_tournament` | Отвязать чат от турнира. |
| `/set_description <текст>` | Описание / правила своего турнира. |
| `/set_channel @channel` | Обязательная подписка на канал для участия. |
| `/clear_channel` | Снять условие подписки. |

### Настройки турнира

| Команда | Что делает |
|---|---|
| `/set_playoff_slots`, `/setplayoffslots`, `/playoff_slots <ID> <N>` | Сколько проходит из каждой группы (1–8). |
| `/set_series_length`, `/setserieslength`, `/series_length <ID> <N>` | Серии бо-N (0/1=одна игра, 3/5/7=серии). |
| `/set_auto_confirm`, `/setautoconfirm`, `/auto_confirm <ID> on\|off` | Мгновенное засчитывание скрина без подтверждения соперника. |
| `/set_third_place`, `/setthirdplace`, `/third_place <ID> on\|off` | Матч за 3-е место (после полуфиналов, параллельно с финалом). По умолчанию `on`. |
| `/skip_third_place`, `/cancel_third_place`, `/skip_bronze [ID]` | Отменить незавершённый матч за 3-е место. Удаляет уже созданные строки бронзы; если финал сыгран — турнир сразу закроется, на подиуме «3-е место (поровну)» между двумя SF-проигравшими. |
| `/set_matches_per_pair`, `/setmatchesperpair`, `/matches_per_pair <ID> group\|playoff <N>` | Сколько игр каждый с каждым / в паре. |
| `/set_reminders`, `/setreminders`, `/reminders <ID> dm <часы>` | Напоминания в DM каждые N часов. |
| `/set_reminders <ID> chat on\|off` | Напоминания в чате (escalating). |
| `/set_reminders <ID> deadline YYYY-MM-DD HH:MM` | Общий дедлайн турнира. |
| `/set_reminders <ID> show` | Текущие настройки напоминаний. |
| `/set_signup_reminder`, `/signup_reminder <ID> <минут>` | Напоминалка о наборе на турнир каждые N минут (0 = выкл). Шлётся в привязанный чат, пока запись открыта и матчи ещё не сгенерированы. |
| `/set_signup_link`, `/signup_link <ID> <URL/текст>` | Ссылка на форму регистрации (или произвольный текст). Подклеивается к каждой напоминалке записи. |
| `/clear_signup_link [ID]` | Снять ссылку на запись. |
| `/set_signup_deadline, /signup_deadline <ID> YYYY-MM-DD HH:MM` | Опциональный дедлайн записи (показывается в напоминалке, не закрывает запись автоматически). |
| `/clear_signup_deadline [ID]` | Снять дедлайн записи. |
| `/set_team`, `/setteam <ID> @user <команда>` | Поставить/обновить метку команды для игрока в турнире. `clear`/`-`/пусто = снять. Имя в чатах/таблицах/сетке отображается как `nick - <команда> (@user)`. |
| `/myteam, /my_team [ID] <команда>` | Игрок сам ставит свою метку (только пока запись открыта). |
| `/award, /title, /give_title @user <титул>` | 👮 Выдать игроку титул (любой текст с эмоджи, например `🐐 GOAT`). Видно в `/profile`, текстовой таблице и `/tablebomb`. |
| `/revoke_award, /revoke_title @user <титул>` | 👮 Снять у игрока титул. |
| `/awards, /titles [@user]` | Список титулов (без аргумента — свои). |
| `/quote <автор>: <текст>` | Добавить цитату в чат (любой). Также работает как reply на сообщение — `/quote` без аргументов возьмёт текст и автора из реплая. |
| `/quotes [N]` | Последние N цитат этого чата (по умолчанию 10). |
| `/delquote <id>` | 👮 Удалить цитату по id. |
| `/set_quote_interval <минут>` | Частота фоновой рассылки цитат в этот чат (0 = выкл). Меняет админ чата или бота. |

### Кастомизация (фон таблиц / сетки)

| Команда | Что делает |
|---|---|
| `/set_tournament_bg`, `/settournamentbg`, `/set_bg`, `/tournament_bg [ID]` | Кастомный фон PNG-картинок (фото с подписью или ответом на фото). |
| `/clear_tournament_bg`, `/cleartournamentbg`, `/clear_bg [ID]` | Удалить фон, вернуть стандартный. |

---

## 👮 Участники по ходу турнира

| Команда | Что делает |
|---|---|
| `/admin_addplayer_late`, `/adminaddplayerlate`, `/addplayer_late`, `/addplayerlate`, `/joinlate`, **`/add_participant`**, `/addparticipant`, **`/add_player`**, `/addplayer` `<tid> <@user> [группа]` | Подсадить игрока в идущий групповой турнир + сразу создать pending-матчи против всех в группе. |
| `/withdraw`, `/kick_player`, `/kickplayer`, **`/remove_participant`**, `/removeparticipant`, **`/remove_player`**, `/removeplayer` `<tid> <@user> [причина]` | Снять игрока с турнира по ходу. Его pending-матчи закрываются как ТП-в-его-сторону, рейтинг и таблица пересчитываются. |
| `/replace_player @old @new [ID]` | (см. выше) Замена записи без потери расписания. |

> Жирным выделены **новые человекочитаемые алиасы** для тех, кто не помнит длинные команды.

---

## 👮 Результаты — админская часть

| Команда | Что делает |
|---|---|
| `/pending`, `/pending_matches`, `/pendingmatches [tid] [@user]` | Список pending-матчей с их ID. |
| `/walkover`, `/tp @loser [@winner] [ID]` | Техническое поражение (0:3). С несколькими pending — кнопочный выбор матча. |
| `/walkover #<match_id> [@loser]` | ТП на конкретный матч. |
| `/walkover_match`, `/walkovermatch <match_id> [@loser]` | Алиас (удобно из логов). |
| `/walkover_all`, `/walkoverall`, `/tp_all`, `/tpall @loser [tid]` | ТП всем оставшимся матчам игрока в турнире (с подтверждением). |
| `/promote`, `/force_advance`, `/advance_player @player [tid]` | Принудительно проводит игрока в следующую стадию плей-офф: закрывает все его открытые leg-и 3:0 и запускает авто-генерацию следующего раунда. |
| `/po_stage_config`, `/po_stage`, `/po_format <stage> <bo3|bo5|bo7|…> [wins|goals] [tid]` | Конфиг плей-офф на стадию: длина серии и что решает (победы / голы). Стадии: `qf`, `sf`, `final`, `r16`, … `/po_stage_config sf bo3 wins` — bo3, первый кто 2 победы (с ранней остановкой). `/po_stage_config sf off` — сброс на дефолт турнира. |
| **`/tech_draw`, `/techdraw`, `/draw`, `/td @p1 @p2 [X:X] [tid]`** | **Техническая ничья** (по умолчанию `1:1`, можно `2:2`, `0:0`, …). |
| **`/tech_draw #<match_id> [X:X]`** | Тех. ничья конкретному матчу по ID. |
| **`/set_deadline`, `/setdeadline`, `/set_dd`, `/setdd`, `/dd`, `/change_deadline`, `/changedeadline #<match_id> +<часы>`** | **Продлить дедлайн матча** на N часов. |
| **`/set_deadline #<match_id> YYYY-MM-DD HH:MM`** | Поставить абсолютный дедлайн матчу (МСК; переопределяется `BOT_DISPLAY_TZ`). |
| **`/set_deadline @p1 @p2 +24 [tid]`** | Дедлайн по паре игроков (бот сам найдёт pending-матч). |
| **`/set_deadline group <tid> +24`** *(или `groups`)* | **Массово**: один дедлайн на все открытые матчи группового этапа. |
| **`/set_deadline playoff <tid> +48`** *(или `po`, `плей-офф`)* | **Массово**: один дедлайн на весь плей-офф (r16+qf+sf+final). |
| **`/set_deadline r16\|qf\|sf\|final <tid> +24`** | Массово по конкретной стадии плей-офф (1/8, 1/4, 1/2, финал). |
| `/admin_report`, `/adminreport`, `/force_report @u1 @u2 3:2 [ID]` | Внести результат за игроков (без подтверждения). |
| `/admin_photo`, `/adminphoto`, `/photo_report`, `/photoreport @u1 @u2 [ID]` | Ответом на фото: OCR распознаёт счёт, записывает матч между указанными игроками. **Поддержка альбомов**: если ответ на фото из альбома — обрабатывает все фото; одинаковый счёт → один матч (голы объединяются), разный счёт → отдельные матчи. |
| `/reocr`, `/re_ocr`, `/tessocr`, `/tess_ocr @u1 @u2 [ID]` | Ответом на фото: пересчитать через **Tesseract** (без AI). Полезно когда AI-модель неправильно распознала ник, но счёт виден. Юзернеймы обоих участников указываются вручную. |
| `/award_points`, `/awardpoints`, `/give_points`, `/givepoints @user N [ID] [причина]` | Выдать / отнять групповые очки (fair-play бонусы). |
| `/edit_goals`, `/editgoals`, `/set_goals`, `/setgoals #<match_id> @sc1 @sc2 …` | Переписать список бомбардиров. `clear` — очистить. |
| `/admin_matchgoals`, `/adminmatchgoals`, `/matchgoals <match_id>` | Список голов матча с goal_id. |
| `/admin_addgoal`, `/adminaddgoal`, `/addgoal <match_id> @user [home\|away] [мин]` | Добавить один гол. |
| `/admin_delgoal`, `/admindelgoal`, `/delgoal <goal_id>` | Удалить гол по id. |
| `/admin_setgoalauthor`, `/adminsetgoalauthor`, `/setgoalauthor <goal_id> @user` | Переназначить автора гола. |
| `/prune_phantoms`, `/prunephantoms`, `/clean_phantoms [tid\|all]` | Удалить «фантомные» pending-матчи. |
| `/ocr_compare`, `/ocrcompare` | Ответом на скрин: прогнать через все OCR/vision-модели + tesseract. |

---

## 👮 ELO / банни / админка пользователей

| Команда | Что делает |
|---|---|
| `/elo @user +50 [причина]` | Изменить ELO на дельту. |
| `/setelo @user 200 [причина]` | Задать абсолютный ELO. |
| `/ban @user [длительность] [причина]` | Бан (24, 7d, perm). |
| `/unban @user` | Снять бан. |
| `/banned` | Список забаненных. |
| `/admin_setnick`, `/adminsetnick`, `/setnick_for`, `/setnickfor @user <nick>` | Назначить игровой ник любому игроку (если указать telegram_id несуществующего игрока — создаст запись). |
| `/admin_addplayer`, `/adminaddplayer`, `/admin_add_player`, `/addplayer <telegram_id> <ник>` | Зарегистрировать игрока без @username по Telegram ID и задать ему ник. |
| `/relink_player`, `/relinkplayer`, `/relink`, `/merge_player`, `/mergeplayer @oldhandle <telegram_id>` | Слить две записи игрока в одну. |
| `/grant_admin`, `/grantadmin @user [коммент]` | Выдать бот-админку. |
| `/revoke_admin`, `/revokeadmin @user` | Снять бот-админку. |
| `/admins` | Список текущих бот-админов. |
| `/add_tadmin`, `/addtadmin <ID> @user` | Назначить админом конкретного турнира. |
| `/remove_tadmin`, `/removetadmin <ID> @user` | Снять админку у турнира. |
| `/tadmins [ID]` | Кто админит конкретный турнир. |
| `/tlog`, `/tournament_log`, `/tournamentlog [ID]` | Аудит-лог действий по турниру (включая `tech_draw`, `set_deadline`). |
| `/broadcast`, `/announce <текст>` | Разослать сообщение участникам. |

---

## ✨ Что нового / поправлено в этой сборке

1. **Скрин матча → админу в DM с кнопками подтверждения.** Когда игрок присылает скрин и матч уходит на проверку, теперь бот шлёт **то же фото** в личку всех админов вместе с `[✅ Засчитать] [❌ Отклонить]`. Если по какой-то причине Телеграм откажется присылать фото — fallback на текстовое сообщение, кнопки те же.
2. **Уведомления о матчах после `/finish_tournament` больше не приходят.** `job_deadline_check` и `job_reminders` теперь явно выкидывают матчи завершённых турниров (`tournaments.stage = 'finished'`) — больше никаких ТП-уведомлений и DM-напоминаний по уже закрытому турниру.
3. **`/tech_draw` — техническая ничья.** Засчитывает матч как ничью (по умолчанию 1:1, можно `0:0`, `2:2`, …). Работает по паре игроков **или** по `#<match_id>`. Логируется в `tlog`.
4. **`/set_deadline` — ручной DD.** Можно выставить дедлайн любого pending-матча и менять его. Поддерживает `+<часы>` (от текущего момента) или абсолютную дату `YYYY-MM-DD HH:MM` в МСК (UTC+3, без DST). Оба игрока получают DM с новым сроком, событие пишется в `tlog`.
   • **Массовая форма по стадии**: `/set_deadline group <tid> +24`, `/set_deadline playoff <tid> +48`, `/set_deadline qf|sf|final|r16 <tid> +24` — обновляет дедлайн сразу всем открытым матчам этапа. Каждому затронутому игроку приходит одно DM-уведомление, без флуда.
5. **Алиасы для участников по ходу турнира.** Команды `/admin_addplayer_late` и `/withdraw` получили человекочитаемые названия `/add_participant` (`/add_player`) и `/remove_participant` (`/remove_player`).
