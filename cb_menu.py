"""
cb_menu.py — Главное меню бота (кнопки «menu:…»).

Вынесено из bot.handle_callback 25.09.2026 без изменения логики — тела веток
перенесены дословно. Общие объекты берутся из bot_core, маршрутизация —
таблица CALLBACK_ROUTES в bot.py.
"""

import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import memory
import bank_ui
import company_ui
import salon_ui
import settings_service

from bot_core import (
    backup,
    deals_menu_keyboard,
    get_menu_text,
    main_menu_keyboard,
    settings_menu_keyboard,
    show_deals_list,
)


async def on_menu(update, context, query, data):
    """Кнопки: callback_data «menu:…»."""
    action = data.split(":", 1)[1]

    if action == "sign_doc":
        context.user_data["awaiting_doc_to_sign"] = True
        await query.edit_message_text(
            "✍️ *Подписать документ*\n\n"
            "Пришли договор или другой документ — PDF или Word.\n"
            "Найду, где стоит наша подпись, поставлю росчерк и печать "
            "и покажу, что получилось.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Меню", callback_data="menu:back")]]),
        )
        return

    if action == "new_deal":
        # Сначала тип сделки: прямая или через салон РФ (субагентская).
        # Документы начинаем ждать после выбора — см. salon_ui (nd:…).
        text, kb = salon_ui.new_deal_choice_screen()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action == "find_deal":
        await query.edit_message_text(
            "🔍 *Поиск сделки*\n\n"
            "Напиши что ищешь:\n"
            "• Номер договора (например: `270625001`)\n"
            "• ФИО покупателя или продавца\n"
            "• VIN автомобиля\n"
            "• Дату договора",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
            ]])
        )
        context.user_data["awaiting_search"] = True
        # Пришли из поиска — из карточки сделки возвращаемся в меню,
        # а не в список за период, открытый когда-то раньше.
        context.user_data.pop("last_deals_view", None)

    elif action == "deals":
        await query.edit_message_text(
            "📋 *Сделки*\n\nВыбери срез:",
            parse_mode="Markdown",
            reply_markup=deals_menu_keyboard(),
        )

    elif action == "active" or action.startswith("active:"):
        # Старая кнопка «Активные сделки». Оставлена для сообщений,
        # которые уже висят в чате со старой разметкой: срез тот же —
        # статусы «активна» + «ждём доплату» без ограничения по периоду.
        page = int(action.split(":")[1]) if ":" in action else 0
        await query.edit_message_text("🔄 Загружаю активные сделки...")
        await show_deals_list(query, context, "all", "active", page)

    elif action == "stats":
        # Подменю выбора периода. Сам расчёт — в обработчике "stats:<period>".
        # «Сегодня» и «вчера» убраны (Илья, 04.09.2026): слишком короткий
        # срез, минимально полезный — неделя. В _resolve_period периоды
        # остались, текстовый запрос «за сегодня» через LLM работает.
        kb = [
            [
                InlineKeyboardButton("📅 Неделя",  callback_data="stats:week"),
                InlineKeyboardButton("📅 Месяц",   callback_data="stats:month"),
            ],
            [
                InlineKeyboardButton("📅 Пр. месяц", callback_data="stats:last_month"),
                InlineKeyboardButton("📅 Квартал",   callback_data="stats:quarter"),
            ],
            [
                InlineKeyboardButton("📅 Год",       callback_data="stats:year"),
                InlineKeyboardButton("📊 Всё время", callback_data="stats:all"),
            ],
            [
                InlineKeyboardButton("📅 Свой период", callback_data="stats:custom"),
            ],
            [
                InlineKeyboardButton("⏳ Ждём доплату", callback_data="stats:pending"),
            ],
            [
                InlineKeyboardButton("◀️ Меню", callback_data="menu:back"),
            ],
        ]
        await query.edit_message_text(
            "📊 *Статистика*\n\nВыбери период:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif action == "bank_profiles":
        text, kb = bank_ui.list_screen()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action == "salons":
        text, kb = salon_ui.list_screen()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action == "company":
        text, kb = company_ui.card_screen()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif action == "memory":
        companies = memory.list_companies()
        bank_profiles = memory.list_bank_profiles()
        instructions = memory.get_instructions()
        lines = [
            "🧠 *Память*\n",
            f"📁 Компаний: {len(companies)}",
            f"🏦 Банковских профилей: {len(bank_profiles)}",
            f"📌 Инструкций: {len(instructions)}",
        ]
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
            ]])
        )

    elif action == "backup":
        # Подменю бэкапов — быстрая сводка и действия.
        latest = await asyncio.to_thread(backup.list_backups, 1)
        if latest:
            last = latest[0]
            summary = (
                f"🕓 Последний бэкап: *{last['created']}*\n"
                f"💾 Размер: {last['size_kb']} KB"
            )
        else:
            summary = "🕓 Бэкапов ещё нет."

        text = (
            "💾 *Бэкапы журнала*\n\n"
            f"{summary}\n\n"
            "_Автоматический бэкап делается каждый день в 03:00_"
        )
        kb = [
            [InlineKeyboardButton("💾 Создать сейчас",     callback_data="backup:create")],
            [InlineKeyboardButton("📋 Последние бэкапы",   callback_data="backup:list")],
            [InlineKeyboardButton("📂 Открыть папку",      callback_data="backup:folder")],
            [InlineKeyboardButton("🗑 Очистить старые",    callback_data="backup:cleanup")],
            [InlineKeyboardButton("◀️ Меню",                callback_data="menu:back")],
        ]
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif action == "settings":
        # Корневое меню настроек — список параметров с текущими значениями.
        lines = ["⚙️ *Настройки*\n"]
        for s in settings_service.SETTINGS:
            lines.append(f"*{s['label']}*")
            lines.append(f"  {settings_service.get_current_label(s)}")
            lines.append("")
        lines.append(
            "_Изменения применяются после автоматического рестарта_\n"
            "_Railway (~30 сек)._"
        )
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=settings_menu_keyboard(),
        )

    elif action == "back":
        context.user_data.pop("current_deal", None)
        await query.edit_message_text(
            get_menu_text(),
            parse_mode="MarkdownV2",
            reply_markup=main_menu_keyboard(),
        )
    return
