# govnl-gf — патч-комплект

Скопируй файлы поверх своего проекта **с сохранением структуры**:

```
bot.py                  → bot.py
playoff_image.py        → playoff_image.py
handlers/tournament.py  → handlers/tournament.py
```

Перезапусти бота. Никаких миграций / зависимостей не требуется.

## Что изменилось

### 1) `/table` и `/playoff` — завершённые турниры теперь скрыты

В пикере по умолчанию видны только активные турниры. Под ними появилась
кнопка **«🏁 Завершённые (N)»**. По нажатию список разворачивается
(с разделителем и кнопкой «↩️ Скрыть завершённые»).

Поведение симметрично для обеих команд (`/table` → `tbl_show_finished`,
`/playoff` → `po_show_finished`).

Если активных турниров нет, кнопка завершённых всё равно появится — так
ничего из истории не теряется.

Затронуто:
* `handlers/tournament.py`: новая функция `_build_tournament_picker_kb()`,
  обновлены `_send_tournament_picker()`, `cb_table_pick()`,
  `cb_playoff_pick()`.
* `bot.py`: callback-роутер пропускает `tbl_show_finished`,
  `tbl_hide_finished`, `po_show_finished`, `po_hide_finished`.

### 2) `/setrowalpha` теперь работает и для `/playoff`

Раньше `row_bg_alpha` влиял только на `standings_image.py`. В
`playoff_image.py` добавлены два хелпера-аналога:

* `_draw_rect_alpha()` — для прямоугольных «шапок» стадий.
* `_draw_rounded_rect_alpha()` — для скруглённых карточек пар (включая
  TBD/done/live и бронзовый матч).

Обводка карточки остаётся непрозрачной — край не размывается, как и в
таблицах. Команда `/setrowalpha <ID> <0-100>` теперь действительно
управляет прозрачностью **и** таблицы, и сетки.

Затронуто:
* `playoff_image.py`:
  * Добавлены `_draw_rect_alpha()`, `_draw_rounded_rect_alpha()`.
  * `_render_image()` и `_render_image_mirrored()` читают
    `t['row_bg_alpha']` и применяют его к header-полоскам стадий +
    карточкам.
  * `_render_card_dyn()` принимает `img=` и `row_alpha=` (опционально,
    backwards-compat).

### 3) `/help` дополнен недостающими командами

Команды, которые были зарегистрированы, но не упоминались в справке,
добавлены в админскую часть help-текста:

* `/set_overlay`, `/setoverlay`, `/overlay`, `/set_transparency`,
  `/settransparency` — прозрачность затемнения фона.
* `/set_row_alpha`, `/setrowalpha`, `/row_alpha`, `/rowalpha` —
  прозрачность строк/карточек.
* `/test_ocr`, `/testocr` — прогнать только tesseract.
* `/edit_match`, `/editmatch` — переписать счёт.
* `/delete_match`, `/deletematch` — удалить матч.
* `/force_confirm`, `/forceconfirm` — принудительное подтверждение.
* `/tmatches`, `/t_matches`, `/tournament_matches` — матчи турнира по
  игрокам.
* `/fill_missing_matches`, `/fillmissing`, `/fill_matches` — добить
  пропущенные pending-матчи группы.

Затронуто: `bot.py` → `ADMIN_ONLY_HELP_TEXT`.
