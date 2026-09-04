"""bank_ui.py — меню «🏦 Реквизиты»: заведение, просмотр, правка, удаление.

Раньше реквизиты заводились только через LLM: она разбирала присланный текст и
молча писала профиль в память — без подтверждения, без проверки форматов и без
понятия «тип счёта». Меню при этом было витриной на три строки.

Состояние формы живёт в user_data бота, а не в диалоге с моделью: многошаговый
ввод, который зависит от того, вспомнит ли LLM свой прошлый вопрос,
разваливается на первом же уточнении.

Профили в callback_data адресуются ИНДЕКСОМ в отсортированном списке, а не
именем: в callback_data всего 64 байта, а названия профилей длинные и
кириллические.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import memory
import bank_requisites as br

logger = logging.getLogger(__name__)

# Валюта подставляется сама и правится кнопкой, а не спрашивается при заведении:
# рублёвый счёт — типовой случай, лишний вопрос в каждой форме себя не окупает.
PREFILLED = ("account_currency",)

HINTS = {
    "account_number": "20 цифр для российского счёта, 16 для киргизского",
    "account_currency": "трёхбуквенный код: RUB, USD",
    "bank_name":      "полное название, как в реквизитах банка",
    "bank_bic":       "только цифры",
    "bank_corr_acc":  "20 цифр",
    "bank_swift":     "8 или 11 символов латиницей; нужен для карточки контрагенту",
    "corr_bank_name": "российский банк, через который проходит платёж",
    "corr_bank_bic":  "9 цифр",
    "corr_bank_acc":  "20 цифр",
}


def _icon(profile: dict) -> str:
    return br.ACCOUNT_TYPE_ICONS[br.resolve_account_type(profile)]


def _names() -> list:
    return memory.list_bank_profiles()


def _by_index(i: int):
    names = _names()
    if 0 <= i < len(names):
        return names[i], memory.get_bank_profile(names[i])
    return None, None


def _back(cb: str = "bp:list"):
    return InlineKeyboardButton("◀️ Назад", callback_data=cb)


def _short(value: str, limit: int = 22) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


# ─── Экраны ─────────────────────────────────────────────────────────────────

def list_screen():
    names = _names()
    rows = [[InlineKeyboardButton("➕ Добавить реквизиты", callback_data="bp:new")]]
    for i, name in enumerate(names):
        rows.append([InlineKeyboardButton(f"{_icon(memory.get_bank_profile(name))} {name}",
                                          callback_data=f"bp:v:{i}")])
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])

    if names:
        text = ("🏦 *Реквизиты*\n\nСчета компании, на которые платит покупатель.\n"
                "Тип счёта определяет шаблон договора и шаблон счёта.")
    else:
        text = ("🏦 *Реквизиты*\n\nПока ни одного профиля.\n"
                "Нажми «Добавить» — спрошу поля по одному.")
    return text, InlineKeyboardMarkup(rows)


def card_screen(index: int):
    name, profile = _by_index(index)
    if not profile:
        return "Профиль не найден.", InlineKeyboardMarkup([[_back()]])

    text = f"{_icon(profile)} *{name}*\n\n```\n{br.describe(profile)}\n```"
    problems = br.validate_profile(profile)
    if problems:
        text += "\n⚠️ " + "\n⚠️ ".join(problems)

    rows = [
        [InlineKeyboardButton("✏️ Изменить", callback_data=f"bp:e:{index}"),
         InlineKeyboardButton("🗑 Удалить",  callback_data=f"bp:d:{index}")],
        [_back()],
    ]
    return text, InlineKeyboardMarkup(rows)


def edit_screen(index: int):
    name, profile = _by_index(index)
    if not profile:
        return "Профиль не найден.", InlineKeyboardMarkup([[_back()]])
    norm = br.normalize(profile)
    rows = [[InlineKeyboardButton(f"{br.FIELD_LABELS[f]}: {_short(norm.get(f) or '—')}",
                                  callback_data=f"bp:f:{index}:{f}")]
            for f in br.FIELD_ORDER[norm["account_type"]]]
    rows.append([InlineKeyboardButton("✏️ Переименовать", callback_data=f"bp:f:{index}:__name__")])
    rows.append([_back(f"bp:v:{index}")])
    return f"*{name}* — что поправить?", InlineKeyboardMarkup(rows)


def draft_screen(draft: dict):
    """Итоговая карточка перед сохранением."""
    norm = br.normalize(draft)
    text = (f"{br.ACCOUNT_TYPE_ICONS[norm['account_type']]} "
            f"*{draft.get('__name__', 'Без названия')}*\n\n"
            f"```\n{br.describe(draft)}\n```")
    rows = []
    problems = br.validate_profile(draft)
    if problems:
        text += "\n⚠️ " + "\n⚠️ ".join(problems)
    else:
        rows.append([InlineKeyboardButton("✅ Сохранить", callback_data="bp:save")])
    rows += [[InlineKeyboardButton(f"✏️ {br.FIELD_LABELS[f]}: {_short(norm.get(f) or '—')}",
                                   callback_data=f"bp:w:{f}")]
             for f in br.FIELD_ORDER[norm["account_type"]]]
    rows.append([InlineKeyboardButton("✏️ Название", callback_data="bp:w:__name__")])
    rows.append([InlineKeyboardButton("❌ Отменить", callback_data="bp:cancel")])
    return text, InlineKeyboardMarkup(rows)


def _ask(field: str):
    """Текст вопроса и клавиатура (у необязательных полей — «Пропустить»)."""
    if field == "__name__":
        return "Название профиля — например «Бакай — через Тинькофф»:", None
    hint = HINTS.get(field, "")
    text = f"*{br.FIELD_LABELS[field]}*" + (f"\n_{hint}_" if hint else "") + "\n\nВведи значение:"
    kb = None
    if field in br.OPTIONAL:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить",
                                                        callback_data="bp:skip")]])
    return text, kb


# ─── Кнопки ─────────────────────────────────────────────────────────────────

async def handle_callback(update, context, data: str) -> bool:
    """True, если кнопка относится к меню реквизитов."""
    if not data.startswith("bp:"):
        return False

    query = update.callback_query
    parts = data.split(":")
    action = parts[1]

    async def show(screen):
        text, kb = screen
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    async def ask(field):
        text, kb = _ask(field)
        context.user_data["bp_wait"] = field
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    if action == "list":
        clear_state(context)
        await show(list_screen())

    elif action == "new":
        await query.edit_message_text(
            "Какой это счёт?\n\n"
            "🇷🇺 *Прямой в РФ* — платёж идёт напрямую в российский банк, "
            "в счёте печатается ИНН РФ с КПП и добавляется QR.\n"
            "🇰🇬 *Через корреспондента* — счёт в банке КР, платёж проходит "
            "через российский банк-корреспондент, в счёте ИНН КР.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🇷🇺 Прямой счёт в РФ", callback_data=f"bp:t:{br.DIRECT_RF}")],
                [InlineKeyboardButton("🇰🇬 Через банк-корреспондент", callback_data=f"bp:t:{br.CORR}")],
                [_back()],
            ]),
        )

    elif action == "t":
        acc_type = parts[2]
        clear_state(context)
        context.user_data["bp_draft"] = {"account_type": acc_type, "account_currency": "RUB"}
        context.user_data["bp_queue"] = [f for f in br.FIELD_ORDER[acc_type]
                                         if f not in PREFILLED]
        await ask("__name__")

    elif action == "v":
        await show(card_screen(int(parts[2])))

    elif action == "e":
        await show(edit_screen(int(parts[2])))

    elif action == "f":                      # правка поля сохранённого профиля
        context.user_data.pop("bp_queue", None)
        context.user_data.pop("bp_draft", None)
        context.user_data["bp_edit_index"] = int(parts[2])
        await ask(parts[3])

    elif action == "w":                      # правка поля черновика
        context.user_data.pop("bp_queue", None)
        context.user_data.pop("bp_edit_index", None)
        await ask(parts[2])

    elif action == "skip":
        field = context.user_data.get("bp_wait")
        if field:
            await _accept(update, context, field, "", from_button=True)

    elif action == "d":
        index = int(parts[2])
        name, _ = _by_index(index)
        await query.edit_message_text(
            f"Удалить профиль *{name}*?\n\n"
            "Сделки, где эти реквизиты уже проставлены, не изменятся.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"bp:dy:{index}")],
                [_back(f"bp:v:{index}")],
            ]),
        )

    elif action == "dy":
        name, _ = _by_index(int(parts[2]))
        if name:
            memory.delete_bank_profile(name)
            logger.info(f"Банковский профиль удалён: {name}")
        await show(list_screen())

    elif action == "save":
        draft = dict(context.user_data.get("bp_draft") or {})
        name = (draft.pop("__name__", "") or "").strip()
        if not name:
            await query.answer("Сначала укажи название", show_alert=True)
            return True
        if br.validate_profile(draft):
            await query.answer("Профиль ещё не полный", show_alert=True)
            return True
        memory.save_bank_profile(name, br.normalize(draft))
        clear_state(context)
        logger.info(f"Банковский профиль сохранён: {name}")
        await show(list_screen())

    elif action == "cancel":
        clear_state(context)
        await show(list_screen())

    return True


# ─── Текстовый ввод ─────────────────────────────────────────────────────────

async def handle_text(update, context, text: str) -> bool:
    """Ловит ответ на вопрос формы. True — текст съеден формой."""
    field = context.user_data.get("bp_wait")
    if not field:
        return False
    return await _accept(update, context, field, (text or "").strip())


async def _accept(update, context, field: str, value: str, from_button: bool = False) -> bool:
    """Принимает значение поля — из текста или из кнопки «Пропустить»."""
    reply = (update.callback_query.message.reply_text if from_button
             else update.message.reply_text)

    async def send(screen):
        text, kb = screen
        await reply(text, parse_mode="Markdown", reply_markup=kb)

    index = context.user_data.get("bp_edit_index")

    # ── Правка одного поля сохранённого профиля
    if index is not None and "bp_draft" not in context.user_data:
        name, profile = _by_index(index)
        if not profile:
            clear_state(context)
            await reply("Профиль не найден.")
            return True

        if field == "__name__":
            if not value:
                await reply("Название не может быть пустым:")
                return True
            if value != name and value in _names():
                await reply("Профиль с таким названием уже есть. Другое название:")
                return True
            memory.delete_bank_profile(name)
            memory.save_bank_profile(value, profile)
            clear_state(context)
            await send(card_screen(_names().index(value)))
            return True

        acc_type = br.resolve_account_type(profile)
        err = br.validate_field(field, value, acc_type) if value else ""
        if err:
            await reply(f"⚠️ {err}\n\nВведи ещё раз:")
            return True
        memory.save_bank_profile(name, br.normalize({**profile, field: value}))
        clear_state(context)
        await send(card_screen(index))
        return True

    # ── Заведение нового профиля
    draft = context.user_data.get("bp_draft")
    if draft is None:
        context.user_data.pop("bp_wait", None)
        return False

    if field == "__name__":
        if not value:
            await reply("Название не может быть пустым:")
            return True
        if value in _names():
            await reply("Профиль с таким названием уже есть. Другое название:")
            return True
        draft["__name__"] = value
    else:
        if value:
            err = br.validate_field(field, value, draft.get("account_type", ""))
            if err:
                await reply(f"⚠️ {err}\n\nВведи ещё раз:")
                return True
        elif field not in br.OPTIONAL:
            await reply("Это поле обязательное. Введи значение:")
            return True
        draft[field] = value

    context.user_data["bp_draft"] = draft
    context.user_data.pop("bp_wait", None)

    queue = context.user_data.get("bp_queue") or []
    if queue:
        next_field = queue.pop(0)
        context.user_data["bp_queue"] = queue
        context.user_data["bp_wait"] = next_field
        text, kb = _ask(next_field)
        await reply(text, parse_mode="Markdown", reply_markup=kb)
        return True

    await send(draft_screen(draft))
    return True


def clear_state(context):
    """Сбрасывает форму — вызывается при клике по любой не-bp кнопке."""
    for key in ("bp_wait", "bp_draft", "bp_queue", "bp_edit_index"):
        context.user_data.pop(key, None)
