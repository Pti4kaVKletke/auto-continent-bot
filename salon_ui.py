"""salon_ui.py — меню «🏬 Салоны» и выбор типа новой сделки.

Кнопки:
  sl:list                 — список салонов
  sl:v:<ИНН>              — карточка салона
  sl:e:<ИНН>              — поля карточки кнопками
  sl:f:<ИНН>:<i>          — правка поля (ждём текст)
  sl:del:<ИНН> / sl:delok:<ИНН> — удаление с подтверждением
  sl:new / sl:new:deal    — новый салон: ждём карточку организации (файл или текст);
                            «:deal» — после сохранения сразу продолжаем новую сделку
  sl:save / sl:de / sl:df:<i> — черновик новой карточки: сохранить / поля / правка поля
  nd:direct / nd:sub / nd:s:<ИНН> — «📄 Новая сделка»: прямая или через салон

Выбор салона для новой сделки хранится у бота (salon.set_pending), а не в
памяти LLM: create_contract сам проставит тип и салон.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import memory
import salon

logger = logging.getLogger(__name__)

NEW_DEAL_DIRECT_TEXT = (
    "📄 *Новая сделка — прямая*\n\n"
    "Отправь документы клиента:\n"
    "• Паспорт РФ покупателя\n"
    "• ТПО и/или таможенную декларацию продавца\n\n"
    "Или напиши данные текстом — я извлеку всё нужное."
)


def start_fresh_deal(chat_id: str) -> None:
    """Новая сделка начинается с чистого листа: история диалога и
    неотправленные сканы прошлой сделки забываются. Иначе LLM дополняет
    новую сделку продавцом, машиной и суммами из прошлой переписки
    (26.09.2026: прислан только паспорт — в сводке оказалась чужая Toyota)."""
    memory.clear_history(chat_id)
    memory.clear_pending_scans(chat_id)


def _kb(rows):
    return InlineKeyboardMarkup(rows)


def _menu_btn():
    return InlineKeyboardButton("◀️ Меню", callback_data="menu:back")


# ─── Экраны ─────────────────────────────────────────────────────────────────

def new_deal_choice_screen():
    text = ("📄 *Новая сделка*\n\n"
            "Какой тип сделки?\n"
            "• *Прямая* — договор с клиентом (физлицо или ИП) напрямую.\n"
            "• *Через салон РФ* — салон нанимает нас субагентом и платит сам.")
    return text, _kb([
        [InlineKeyboardButton("👤 Прямая", callback_data="nd:direct")],
        [InlineKeyboardButton("🏬 Через салон РФ (субагент)", callback_data="nd:sub")],
        [_menu_btn()],
    ])


def pick_salon_screen():
    rows = [[InlineKeyboardButton(f"🏬 {salon.title(c)}", callback_data=f"nd:s:{inn}")]
            for inn, c in salon.list_all()]
    rows.append([InlineKeyboardButton("➕ Новый салон", callback_data="sl:new:deal")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:new_deal")])
    text = "🏬 *Через какой салон?*" if len(rows) > 2 else \
        "🏬 *Салонов пока нет.* Добавь первый — пришли его карточку организации."
    return text, _kb(rows)


def subagent_docs_text(card: dict) -> str:
    return (
        "📄 *Новая сделка — через салон*\n"
        f"🏬 Салон (Агент): {salon.journal_value(card)}\n\n"
        "Отправь документы конечного покупателя и машины:\n"
        "• Паспорт РФ покупателя\n"
        "• ТПО и/или таможенную декларацию продавца\n\n"
        "Номер и дату договора салона с клиентом спрошу в конце."
    )


def list_screen():
    rows = [[InlineKeyboardButton(f"🏬 {salon.title(c)}", callback_data=f"sl:v:{inn}")]
            for inn, c in salon.list_all()]
    rows.append([InlineKeyboardButton("➕ Добавить салон", callback_data="sl:new")])
    rows.append([_menu_btn()])
    text = ("🏬 *Салоны* (Агент РФ в субагентских сделках)\n\n"
            + ("Выбери салон или добавь новый." if len(rows) > 2 else "Пока ни одного салона."))
    return text, _kb(rows)


def _card_text(card: dict, title: str) -> str:
    text = f"{title}\n\n```\n{salon.describe(card)}\n```"
    missing = salon.problems(card)
    if missing:
        text += "\n⚠️ не заполнено: " + ", ".join(missing)
    return text


def card_screen(inn: str):
    card = salon.get(inn)
    if not card:
        return "Салон не найден.", _kb([[InlineKeyboardButton("◀️ К списку", callback_data="sl:list")]])
    return _card_text(card, "🏬 *Карточка салона*"), _kb([
        [InlineKeyboardButton("✏️ Изменить", callback_data=f"sl:e:{inn}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"sl:del:{inn}")],
        [InlineKeyboardButton("◀️ К списку", callback_data="sl:list")],
    ])


def _field_rows(card: dict, cb_prefix: str) -> list:
    rows = []
    for i, (key, label, ooo_only) in enumerate(salon.FIELDS):
        if card.get("org_form") == salon.ORG_IP and ooo_only:
            continue
        value = card.get(key) or "—"
        value = value if len(value) <= 24 else value[:23] + "…"
        rows.append([InlineKeyboardButton(f"{label}: {value}", callback_data=f"{cb_prefix}{i}")])
    return rows


def edit_screen(inn: str):
    card = salon.get(inn)
    rows = _field_rows(card, f"sl:f:{inn}:")
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"sl:v:{inn}")])
    return "✏️ *Что поправить?*", _kb(rows)


def draft_screen(context):
    draft = context.user_data.get("sl_draft") or {}
    card = salon.normalize(draft.get("card") or {})
    return _card_text(card, "🏬 *Новый салон — проверь данные*"), _kb([
        [InlineKeyboardButton("✅ Сохранить", callback_data="sl:save")],
        [InlineKeyboardButton("✏️ Поправить поле", callback_data="sl:de")],
        [InlineKeyboardButton("❌ Отмена", callback_data="sl:list")],
    ])


def draft_edit_screen(context):
    card = salon.normalize((context.user_data.get("sl_draft") or {}).get("card") or {})
    rows = _field_rows(card, "sl:df:")
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="sl:dv")])
    return "✏️ *Что поправить?*", _kb(rows)


# ─── Кнопки ─────────────────────────────────────────────────────────────────

async def handle_callback(update, context, data: str) -> bool:
    if not (data.startswith("sl:") or data.startswith("nd:")):
        return False
    query = update.callback_query
    chat_id = str(update.effective_chat.id)
    parts = data.split(":")

    async def show(screen):
        text, kb = screen
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    # Любая кнопка, кроме правки поля, отменяет ожидание текста для поля.
    context.user_data.pop("sl_wait_field", None)
    if not data.startswith("sl:new"):
        context.user_data.pop("sl_wait_new", None)

    # ── Новая сделка ──
    if data == "nd:direct":
        salon.clear_pending(chat_id)
        start_fresh_deal(chat_id)
        context.user_data["awaiting_new_deal_docs"] = 1
        await show((NEW_DEAL_DIRECT_TEXT, _kb([[_menu_btn()]])))
        return True
    if data == "nd:sub":
        await show(pick_salon_screen())
        return True
    if data.startswith("nd:s:"):
        inn = parts[2]
        card = salon.get(inn)
        missing = salon.problems(card) if card else ["карточка не найдена"]
        if missing:
            await show((_card_text(card, "⚠️ *Карточку салона нужно дополнить*") if card
                        else "Салон не найден.",
                        _kb([[InlineKeyboardButton("✏️ Дополнить", callback_data=f"sl:e:{inn}")],
                             [InlineKeyboardButton("◀️ Назад", callback_data="nd:sub")]])))
            return True
        salon.set_pending(chat_id, inn)
        start_fresh_deal(chat_id)
        context.user_data["awaiting_new_deal_docs"] = 1
        await show((subagent_docs_text(card), _kb([[_menu_btn()]])))
        return True

    # ── Справочник ──
    action = parts[1]
    if action == "list":
        context.user_data.pop("sl_draft", None)
        await show(list_screen())
    elif action == "v":
        await show(card_screen(parts[2]))
    elif action == "e":
        await show(edit_screen(parts[2]))
    elif action == "f":
        inn, idx = parts[2], int(parts[3])
        key, label, _ = salon.FIELDS[idx]
        context.user_data["sl_wait_field"] = (inn, key)
        current = salon.get(inn).get(key, "")
        await query.edit_message_text(
            f"*{label}*\n\n" + (f"Сейчас: `{current}`\n\n" if current else "")
            + "Введи новое значение (или «-», чтобы очистить):",
            parse_mode="Markdown")
    elif action == "del":
        inn = parts[2]
        await show((f"Удалить салон {salon.title(salon.get(inn)) if salon.get(inn) else inn}? "
                    "Сделки в журнале не изменятся, но документы по ним без карточки "
                    "не соберутся.",
                    _kb([[InlineKeyboardButton("🗑 Да, удалить", callback_data=f"sl:delok:{inn}")],
                         [InlineKeyboardButton("◀️ Нет", callback_data=f"sl:v:{inn}")]])))
    elif action == "delok":
        import memory
        memory.delete_salon(parts[2])
        await show(list_screen())
    elif action == "new":
        context.user_data["sl_wait_new"] = {"for_deal": len(parts) > 2 and parts[2] == "deal"}
        await query.edit_message_text(
            "🏬 *Новый салон*\n\n"
            "Пришли карточку организации салона (PDF, фото или Word) или выписку "
            "ЕГРЮЛ/ЕГРИП — либо напиши реквизиты текстом. Я заполню карточку и "
            "покажу на проверку.",
            parse_mode="Markdown",
            reply_markup=_kb([[InlineKeyboardButton("◀️ Отмена", callback_data="sl:list")]]))
    elif action == "dv":
        await show(draft_screen(context))
    elif action == "de":
        await show(draft_edit_screen(context))
    elif action == "df":
        key, label, _ = salon.FIELDS[int(parts[2])]
        context.user_data["sl_wait_field"] = ("__draft__", key)
        await query.edit_message_text(f"*{label}*\n\nВведи значение (или «-», чтобы очистить):",
                                      parse_mode="Markdown")
    elif action == "save":
        draft = context.user_data.get("sl_draft") or {}
        try:
            card = salon.save(draft.get("card") or {})
        except ValueError as e:
            await show((f"⚠️ Не сохранено: {e}", draft_screen(context)[1]))
            return True
        context.user_data.pop("sl_draft", None)
        if draft.get("for_deal"):
            if salon.problems(card):
                await show((_card_text(card, "⚠️ *Салон сохранён, но карточку нужно дополнить*"),
                            _kb([[InlineKeyboardButton("✏️ Дополнить", callback_data=f"sl:e:{card['inn']}")],
                                 [InlineKeyboardButton("◀️ К выбору салона", callback_data="nd:sub")]])))
                return True
            salon.set_pending(chat_id, card["inn"])
            start_fresh_deal(chat_id)
            context.user_data["awaiting_new_deal_docs"] = 1
            await show((subagent_docs_text(card), _kb([[_menu_btn()]])))
        else:
            await show(card_screen(card["inn"]))
    return True


# ─── Текст и файлы ──────────────────────────────────────────────────────────

async def _extract_and_show(message, context, agent, filepath=None, filename=None, text=""):
    wait = context.user_data.pop("sl_wait_new", None) or {}
    status = await message.reply_text("📥 Читаю карточку салона...")
    try:
        card = await agent.extract_salon_card(filepath=filepath, filename=filename, text=text)
    except Exception as e:
        logger.error(f"Карточка салона не распознана: {e}", exc_info=True)
        card = salon.normalize({})
    try:
        await status.delete()
    except Exception:
        pass
    context.user_data["sl_draft"] = {"card": card, "for_deal": bool(wait.get("for_deal"))}
    text_, kb = draft_screen(context)
    await message.reply_text(text_, parse_mode="Markdown", reply_markup=kb)


async def handle_file(update, context, agent, filepath: str, filename: str) -> bool:
    if not context.user_data.get("sl_wait_new"):
        return False
    await _extract_and_show(update.message, context, agent,
                            filepath=filepath, filename=filename,
                            text=update.message.caption or "")
    return True


async def handle_text(update, context, agent, text: str) -> bool:
    wait_field = context.user_data.get("sl_wait_field")
    if wait_field:
        inn, key = wait_field
        context.user_data.pop("sl_wait_field", None)
        value = (text or "").strip()
        if value == "-":
            value = ""
        if inn == "__draft__":
            draft = context.user_data.setdefault("sl_draft", {"card": {}, "for_deal": False})
            draft["card"] = salon.normalize({**draft.get("card", {}), key: value})
            text_, kb = draft_screen(context)
        else:
            card = {**salon.get(inn), key: value}
            new = salon.normalize(card)
            if key == "inn" and new["inn"] != inn:
                import memory
                memory.delete_salon(inn)
            salon.save(new)
            text_, kb = card_screen(new["inn"])
        await update.message.reply_text(text_, parse_mode="Markdown", reply_markup=kb)
        return True
    if context.user_data.get("sl_wait_new"):
        await _extract_and_show(update.message, context, agent, text=text)
        return True
    return False


def clear_state(context):
    for k in ("sl_wait_field", "sl_wait_new", "sl_draft"):
        context.user_data.pop(k, None)
