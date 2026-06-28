import os
import logging
import asyncio
import random
import time
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from agent import DocumentAgent
import memory

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

agent = DocumentAgent()

# ─── КОНТРОЛЬ ДОСТУПА ────────────────────────────────────────────────────────

ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "268470621"))

async def check_access(update: Update) -> bool:
    allowed = set(
        int(x.strip())
        for x in os.environ.get("ALLOWED_CHAT_IDS", str(ALLOWED_CHAT_ID)).split(",")
        if x.strip().lstrip("-").isdigit()
    )
    return update.effective_chat.id in allowed


# ─── TYPING INDICATOR ────────────────────────────────────────────────────────

async def typing_while(chat_id, context: ContextTypes.DEFAULT_TYPE, coro):
    """Показывает анимацию печати пока выполняется coro."""
    stop = asyncio.Event()

    async def _keep_typing():
        while not stop.is_set():
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(_keep_typing())
    try:
        result = await coro
    finally:
        stop.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
    return result


# ─── ГЛАВНОЕ МЕНЮ ────────────────────────────────────────────────────────────

_GREETINGS = [
    "👋 Привет\\! Я готова к работе\\.",
    "🤝 На связи\\! Чем могу помочь\\?",
    "✨ Готова\\! Выбери действие или напиши что нужно\\.",
    "🚗 На месте\\! Что делаем\\?",
    "💼 Готова к работе\\. Выбери действие\\.",
    "👌 Здесь\\! Чем займёмся\\?",
]

def get_menu_text() -> str:
    return random.choice(_GREETINGS)

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Новая сделка",    callback_data="menu:new_deal"),
            InlineKeyboardButton("🔍 Найти сделку",    callback_data="menu:find_deal"),
        ],
        [
            InlineKeyboardButton("✅ Активные сделки", callback_data="menu:active"),
            InlineKeyboardButton("📊 Статистика",      callback_data="menu:stats"),
        ],
        [
            InlineKeyboardButton("🏦 Реквизиты",       callback_data="menu:bank_profiles"),
            InlineKeyboardButton("🧠 Память",          callback_data="menu:memory"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text(
        get_menu_text(),
        parse_mode="MarkdownV2",
        reply_markup=main_menu_keyboard(),
    )


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        get_menu_text(),
        parse_mode="MarkdownV2",
        reply_markup=main_menu_keyboard(),
    )


async def show_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companies = memory.list_companies()
    instructions = memory.get_instructions()

    text = "🧠 Моя память:\n\n"

    if companies:
        text += "Сохранённые компании:\n"
        for c in companies:
            text += f"  • {c['name']}\n"
    else:
        text += "Компаний пока нет\n"

    text += "\n"

    bank_profiles = memory.list_bank_profiles()
    if bank_profiles:
        text += "Банковские профили:\n"
        for name in bank_profiles:
            text += f"  • {name}\n"
    else:
        text += "Банковских профилей пока нет\n"

    text += "\n"

    if instructions:
        text += "Постоянные инструкции:\n"
        for i in instructions:
            text += f"  {i['id']}. {i['text']}\n"
        text += "\nЧтобы удалить инструкцию: /del_instruction 1"
    else:
        text += "Инструкций пока нет"

    await update.message.reply_text(text)


async def del_instruction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        instruction_id = int(context.args[0])
        memory.delete_instruction(instruction_id)
        await update.message.reply_text(f"✅ Инструкция #{instruction_id} удалена")
    except (IndexError, ValueError):
        await update.message.reply_text("Укажите номер инструкции: /del_instruction 1")


async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memory.clear_history()
    await update.message.reply_text("✅ История диалога очищена")


# ─── ОТПРАВКА РЕЗУЛЬТАТА ─────────────────────────────────────────────────────

async def send_result(message, result: dict):
    """Отправляет файлы и текст результата."""
    if result.get("files"):
        for f_info in result["files"]:
            try:
                with open(f_info["file"], "rb") as f:
                    link = f_info.get("drive_link", "")
                    caption = f"☁️ {link}" if link else "☁️ (не загружено на Drive)"
                    await message.reply_document(
                        document=f,
                        filename=f_info["filename"],
                        caption=caption,
                    )
            except FileNotFoundError:
                logger.error(f"Файл не найден при отправке: {f_info.get('file')}")

    if result.get("text"):
        reply_markup = None
        if result.get("buttons"):
            keyboard = [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])]
                for b in result["buttons"]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text(result["text"], reply_markup=reply_markup)


# ─── ОБРАБОТКА ФАЙЛОВ ────────────────────────────────────────────────────────

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    message = update.message
    chat_id = str(update.effective_chat.id)
    await message.reply_text("📥 Получил файл, обрабатываю...")

    if message.document:
        file = await message.document.get_file()
        filename = message.document.file_name
    elif message.photo:
        file = await message.photo[-1].get_file()
        filename = "photo.jpg"
    else:
        await message.reply_text("❌ Неподдерживаемый тип файла")
        return

    scans_dir = Path("/data/pending_scans")
    scans_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = f"{int(time.time())}_{filename}"
    filepath = str(scans_dir / safe_filename)
    await file.download_to_drive(filepath)

    memory.add_pending_scan(chat_id, filepath, filename)
    logger.info(f"Скан сохранён: {filepath} (chat_id={chat_id})")

    caption = message.caption or ""
    result = await typing_while(
        update.effective_chat.id, context,
        agent.process_file(filepath, filename, caption, chat_id=chat_id)
    )
    await send_result(message, result)


# ─── ОБРАБОТКА CALLBACK-КНОПОК ───────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        await query.message.reply_text("⛔ Доступ запрещён.")
        return

    data = query.data or ""

    # ── Главное меню ──────────────────────────────────────────────────────────
    if data.startswith("menu:"):
        action = data.split(":", 1)[1]

        if action == "new_deal":
            await query.edit_message_text(
                "📄 *Новая сделка*\n\n"
                "Отправь документы клиента:\n"
                "• Паспорт РФ покупателя\n"
                "• ТПО и/или таможенную декларацию продавца\n\n"
                "Или напиши данные текстом — я извлеку всё нужное.",
                parse_mode="Markdown",
            )

        elif action == "find_deal":
            await query.edit_message_text(
                "🔍 *Поиск сделки*\n\n"
                "Напиши что ищешь:\n"
                "• Номер договора (например: `270625001`)\n"
                "• ФИО покупателя или продавца\n"
                "• VIN автомобиля\n"
                "• Дату договора",
                parse_mode="Markdown",
            )
            context.user_data["awaiting_search"] = True

        elif action == "active":
            await query.edit_message_text("🔄 Загружаю активные сделки...")
            deals = await agent.sheets.find_deal("активна")
            if not deals:
                await query.edit_message_text(
                    "Активных сделок нет.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                    ]])
                )
            else:
                lines = [f"🔄 *Активные сделки: {len(deals)}*\n"]
                keyboard = []
                for d in deals:
                    num   = d.get("Номер договора", "—")
                    buyer = d.get("buyer_name", "—")
                    car   = d.get("car_model", "—")
                    date  = d.get("Дата договора", "")
                    lines.append(f"📄 `{num}` · {buyer} · {car}")
                    keyboard.append([InlineKeyboardButton(
                        f"📄 {num} — {buyer[:20]}",
                        callback_data=f"dealaction:{num}:menu"
                    )])
                keyboard.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])
                await query.edit_message_text(
                    "\n".join(lines),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        elif action == "stats":
            await query.edit_message_text("📊 Считаю статистику...")
            result = await typing_while(
                update.effective_chat.id, context,
                agent.process_message("покажи статистику сделок за этот месяц", chat_id=str(update.effective_chat.id))
            )
            await query.message.reply_text(
                result.get("text") or "Нет данных.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                ]])
            )

        elif action == "bank_profiles":
            profiles = memory.list_bank_profiles()
            if profiles:
                lines = ["🏦 *Банковские профили:*\n"]
                for name in profiles:
                    d = memory.get_bank_profile(name)
                    lines.append(f"*{name}*")
                    if d.get("account_number"):
                        lines.append(f"  Счёт: `{d['account_number']}`")
                    if d.get("bank_ben_line1"):
                        lines.append(f"  Банк: {d['bank_ben_line1']}")
                    lines.append("")
                text = "\n".join(lines)
            else:
                text = "🏦 Банковских профилей пока нет.\n\nДобавь реквизиты при создании следующей сделки."
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                ]])
            )

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

        elif action == "back":
            await query.edit_message_text(
                get_menu_text(),
                parse_mode="MarkdownV2",
                reply_markup=main_menu_keyboard(),
            )
        return

    # ── Выбор даты ────────────────────────────────────────────────────────────
    if data.startswith("deal_date:"):
        value = data.split(":", 1)[1]
        await query.edit_message_reply_markup(reply_markup=None)

        if value == "__custom__":
            context.user_data["awaiting_deal_date"] = True
            await query.message.reply_text(
                "Введите дату договора в формате ДД.ММ.ГГГГ (например: 18.06.2026):"
            )
        else:
            result = await typing_while(
                update.effective_chat.id, context,
                agent.process_message(f"Дата договора: {value}", chat_id=str(update.effective_chat.id))
            )
            await send_result(query.message, result)

    # ── Меню документов сделки ────────────────────────────────────────────────
    elif data.startswith("docmenu:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            contract_number = parts[1]
            doc_type        = parts[2]
            await query.edit_message_reply_markup(reply_markup=None)
            result = await typing_while(
                update.effective_chat.id, context,
                agent.process_message(
                    f"Создай документы для сделки {contract_number}, тип: {doc_type}",
                    chat_id=str(update.effective_chat.id),
                )
            )
            await send_result(query.message, result)

    # ── Меню действий по сделке ──────────────────────────────────────────────
    elif data.startswith("dealaction:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            return
        num    = parts[1]
        action = parts[2]

        if action == "menu":
            # Загружаем данные сделки и показываем меню действий
            deal = await agent.sheets.get_deal(num)
            if not deal:
                await query.edit_message_text(f"❌ Сделка {num} не найдена.")
                return
            buyer  = deal.get("buyer_name", "—")
            seller = deal.get("seller_name", "—")
            car    = deal.get("car_model", "—")
            vin    = deal.get("car_vin", "—")
            price  = deal.get("car_price", "—")
            date   = deal.get("Дата договора", "—")
            folder = deal.get("Папка Drive", "")
            text = (
                f"📄 *Сделка {num}* от {date}\n\n"
                f"👤 {buyer}\n"
                f"👤 {seller}\n"
                f"🚗 {car} · VIN `{vin}`\n"
                f"💰 {price} руб."
            )
            keyboard = [
                [InlineKeyboardButton("📋 Создать документы", callback_data=f"dealaction:{num}:docs")],
                [InlineKeyboardButton("✅ Завершить сделку",  callback_data=f"dealaction:{num}:complete")],
                [InlineKeyboardButton("❌ Отменить сделку",   callback_data=f"dealaction:{num}:cancel")],
            ]
            if folder:
                keyboard.insert(0, [InlineKeyboardButton("📁 Открыть на Drive", url=folder)])
            keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:active")])
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif action == "docs":
            await query.edit_message_text(f"⏳ Загружаю данные сделки {num}...")
            result = await typing_while(
                update.effective_chat.id, context,
                agent.process_message(f"проверь сделку {num}", chat_id=str(update.effective_chat.id))
            )
            await send_result(query.message, result)

        elif action == "complete":
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Да, завершить", callback_data=f"dealaction:{num}:complete_yes"),
                    InlineKeyboardButton("◀️ Отмена",        callback_data=f"dealaction:{num}:menu"),
                ]
            ])
            await query.edit_message_text(
                f"Завершить сделку *{num}*?\nДеньги получены, авто передано.",
                parse_mode="Markdown",
                reply_markup=keyboard
            )

        elif action == "complete_yes":
            ok = await agent.sheets.update_deal(num, {"Статус": "завершена"})
            await query.edit_message_text(
                f"✅ Сделка {num} завершена." if ok else f"❌ Не удалось завершить сделку {num}.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                ]])
            )

        elif action == "cancel":
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("❌ Да, отменить", callback_data=f"dealaction:{num}:cancel_yes"),
                    InlineKeyboardButton("◀️ Назад",        callback_data=f"dealaction:{num}:menu"),
                ]
            ])
            await query.edit_message_text(
                f"Отменить сделку *{num}*?",
                parse_mode="Markdown",
                reply_markup=keyboard
            )

        elif action == "cancel_yes":
            ok = await agent.sheets.cancel_deal(num)
            await query.edit_message_text(
                f"✅ Сделка {num} отменена." if ok else f"❌ Не удалось отменить сделку {num}.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                ]])
            )

    # ── Выбор банковского профиля ─────────────────────────────────────────────
    elif data.startswith("bankprofile:"):
        profile_name = data.split(":", 1)[1]
        if profile_name == "__new__":
            user_text = "Использовать новые реквизиты (введу их сейчас)"
        else:
            user_text = f"Использовать сохранённые реквизиты: {profile_name}"
        await query.edit_message_reply_markup(reply_markup=None)
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(user_text, chat_id=str(update.effective_chat.id))
        )
        await send_result(query.message, result)


# ─── ОБРАБОТКА ТЕКСТА ────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    user_text = update.message.text
    chat_id = str(update.effective_chat.id)

    if context.user_data.get("awaiting_deal_date"):
        context.user_data["awaiting_deal_date"] = False
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(f"Дата договора: {user_text.strip()}", chat_id=chat_id)
        )
        await send_result(update.message, result)
        return

    if context.user_data.get("awaiting_search"):
        context.user_data["awaiting_search"] = False
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(f"найди сделку: {user_text}", chat_id=chat_id)
        )
        await send_result(update.message, result)
        return

    result = await typing_while(
        update.effective_chat.id, context,
        agent.process_message(user_text, chat_id=chat_id)
    )
    await send_result(update.message, result)


# ─── ОБРАБОТЧИК ОШИБОК ───────────────────────────────────────────────────────

async def error_handler(update, context):
    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Произошла ошибка, попробуйте ещё раз.")
        except Exception:
            pass


# ─── ЗАПУСК ──────────────────────────────────────────────────────────────────

def main():
    memory.init_db()
    memory.cleanup_old_pending_scans()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start",           start))
    app.add_handler(CommandHandler("menu",            show_main_menu))
    app.add_handler(CommandHandler("memory",          show_memory))
    app.add_handler(CommandHandler("clear",           clear_history))
    app.add_handler(CommandHandler("del_instruction", del_instruction))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
