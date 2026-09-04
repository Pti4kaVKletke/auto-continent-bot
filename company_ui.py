"""company_ui.py — меню «🏢 Компания»: карточка компании и её правка.

Данные компании раньше были вписаны текстом в семь шаблонов и двумя
константами в doc_builder. Здесь они лежат в одном месте, правятся по одному
полю кнопкой, и отсюда же собирается карточка для контрагента — та же
компания плюс реквизиты выбранного счёта.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import memory
import company
import bank_requisites as br

logger = logging.getLogger(__name__)


def _back(cb: str = "co:card"):
    return InlineKeyboardButton("◀️ Назад", callback_data=cb)


def _short(value: str, limit: int = 26) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


# ─── Экраны ─────────────────────────────────────────────────────────────────

def card_screen():
    text = "🏢 *Карточка компании*\n\n```\n" + company.describe(memory.get_setting) + "\n```"
    missing = company.problems(memory.get_setting)
    if missing:
        text += "\n⚠️ не заполнено: " + ", ".join(missing)
    rows = [
        [InlineKeyboardButton("✏️ Изменить", callback_data="co:edit")],
        [InlineKeyboardButton("📇 Карточка контрагенту", callback_data="co:send")],
        [InlineKeyboardButton("◀️ Меню", callback_data="menu:back")],
    ]
    return text, InlineKeyboardMarkup(rows)


def edit_screen(page: int = 0):
    """Поля карточки кнопками. Их 18 — режем на страницы по 9."""
    fields = list(enumerate(company.FIELDS))
    per_page = 9
    rows = []
    for i, (key, label, _, _) in fields[page * per_page:(page + 1) * per_page]:
        value = company.get(key, memory.get_setting) or "—"
        rows.append([InlineKeyboardButton(f"{label}: {_short(value)}",
                                          callback_data=f"co:f:{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"co:edit:{page - 1}"))
    if (page + 1) * per_page < len(fields):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"co:edit:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_back()])
    return "🏢 *Что поправить?*", InlineKeyboardMarkup(rows)


def profiles_screen():
    names = memory.list_bank_profiles()
    if not names:
        return ("Ни одного банковского профиля нет — сначала заведи реквизиты "
                "в меню 🏦 Реквизиты."), InlineKeyboardMarkup([[_back()]])
    rows = [[InlineKeyboardButton(
        f"{br.ACCOUNT_TYPE_ICONS[br.resolve_account_type(memory.get_bank_profile(n))]} {n}",
        callback_data=f"co:s:{i}")] for i, n in enumerate(names)]
    rows.append([_back()])
    return "По какому счёту сделать карточку?", InlineKeyboardMarkup(rows)


# ─── Карточка для контрагента ───────────────────────────────────────────────

def contractor_card_rows(profile_name: str) -> list:
    """Состав карточки одним списком: пары («Подпись», значение),
    («Заголовок», None) для заголовка блока и None для пустой строки.

    Один список на оба представления — текст в чат и файл. Иначе состав
    карточки пришлось бы держать в двух местах и они бы разошлись.
    """
    c = company.card(memory.get_setting)
    bank = br.normalize(memory.get_bank_profile(profile_name) or {})
    rows = []

    def add(label, value):
        if value:
            rows.append((label, value))

    add("Наименование", c["company_name"])
    add("Полное наименование", c["company_name_full"])
    add("ИНН", c["company_inn"])
    add("ОКПО", c["company_okpo"])
    add("Регистрационный номер", c["company_reg_number"])
    add("Дата регистрации", c["company_reg_date"])
    add("ИНН РФ", c["company_inn_rf"])
    add("КПП", c["company_kpp_rf"])
    add("Юридический адрес", c["company_address"])
    add("Фактический адрес", c["company_address_fact"])
    add("Телефон", c["company_phone"])
    add("E-mail", c["company_email"])
    add(c["director_position"], c["director_name"])

    rows.append(None)
    rows.append((f"Банковские реквизиты — {br.ACCOUNT_TYPE_LABELS[bank['account_type']]}", None))
    add("Получатель", c["company_name"])
    add("Номер счёта", bank["account_number"])
    add("Валюта счёта", bank["account_currency"])
    add("Банк", bank["bank_name"])
    add("БИК", bank["bank_bic"])
    add("Корр. счёт", bank["bank_corr_acc"])
    add("SWIFT", bank["bank_swift"])
    if bank["account_type"] == br.CORR:
        add("Банк-корреспондент", bank["corr_bank_name"])
        add("БИК корреспондента", bank["corr_bank_bic"])
        add("Корр. счёт корреспондента", bank["corr_bank_acc"])
    return rows


def contractor_card_text(profile_name: str) -> str:
    """Та же карточка обычным текстом — переслать в переписку с телефона."""
    lines = []
    for item in contractor_card_rows(profile_name):
        if item is None:
            lines.append("")
        elif item[1] is None:
            lines.append(f"*{item[0]}*")
        else:
            lines.append(f"{item[0]}: {item[1]}")
    return "\n".join(lines)


# ─── Кнопки ─────────────────────────────────────────────────────────────────

async def handle_callback(update, context, data: str, on_send=None) -> bool:
    """True, если кнопка относится к меню компании.

    on_send — колбэк(profile_name), которым бот отдаёт готовый файл карточки:
    сборка документа живёт в doc_builder, а не здесь.
    """
    if not data.startswith("co:"):
        return False

    query = update.callback_query
    parts = data.split(":")
    action = parts[1]

    async def show(screen):
        text, kb = screen
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    if action == "card":
        clear_state(context)
        await show(card_screen())

    elif action == "edit":
        page = int(parts[2]) if len(parts) > 2 else 0
        await show(edit_screen(page))

    elif action == "f":
        idx = int(parts[2])
        key, label, placeholder, _ = company.FIELDS[idx]
        context.user_data["co_wait"] = key
        current = company.get(key, memory.get_setting)
        await query.edit_message_text(
            f"*{label}*\n_плейсхолдер {placeholder}_\n\n"
            + (f"Сейчас: `{current}`\n\n" if current else "")
            + "Введи новое значение (или «-», чтобы очистить):",
            parse_mode="Markdown",
        )

    elif action == "send":
        await show(profiles_screen())

    elif action == "s":
        names = memory.list_bank_profiles()
        idx = int(parts[2])
        if 0 <= idx < len(names) and on_send:
            await on_send(names[idx])

    return True


async def handle_text(update, context, text: str) -> bool:
    key = context.user_data.get("co_wait")
    if not key:
        return False
    value = (text or "").strip()
    if value == "-":
        value = ""
    memory.set_setting(key, value)
    context.user_data.pop("co_wait", None)
    logger.info(f"Карточка компании: поле {key} обновлено")
    screen_text, kb = card_screen()
    await update.message.reply_text(screen_text, parse_mode="Markdown", reply_markup=kb)
    return True


def clear_state(context):
    context.user_data.pop("co_wait", None)
