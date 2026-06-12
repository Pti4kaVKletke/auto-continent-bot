import os
import logging
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from agent import DocumentAgent
import memory

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

agent = DocumentAgent()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я агент Авто Континент.\n\n"
        "Просто пишите мне что нужно сделать — я пойму.\n\n"
        "Например:\n"
        "• Скиньте файл с реквизитами клиента\n"
        "• «Сохрани реквизиты: ООО Ромашка, ИНН 7701234567...»\n"
        "• «Создай договор для ООО Ромашка»\n"
        "• «Всегда добавляй НДС 20% в счета»\n\n"
        "/memory — что я помню\n"
        "/clear — очистить историю диалога"
    )


async def show_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companies = memory.list_companies()
    instructions = memory.get_instructions()

    text = "🧠 *Моя память:*\n\n"

    if companies:
        text += "*Сохранённые компании:*\n"
        for c in companies:
            text += f"  • {c['name']}\n"
    else:
        text += "Компаний пока нет\n"

    text += "\n"

    bank_profiles = memory.list_bank_profiles()
    if bank_profiles:
        text += "*Банковские профили:*\n"
        for name in bank_profiles:
            text += f"  • {name}\n"
    else:
        text += "Банковских профилей пока нет\n"

    text += "\n"

    if instructions:
        text += "*Постоянные инструкции:*\n"
        for i in instructions:
            text += f"  {i['id']}. {i['text']}\n"
        text += "\nЧтобы удалить инструкцию: /del_instruction 1"
    else:
        text += "Инструкций пока нет"

    await update.message.reply_text(text, parse_mode="Markdown")


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


async def send_result(message, result: dict):
    """Отправляет файлы и текст результата обработки сообщению пользователя."""
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


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    await message.reply_text("📥 Получил файл, обрабатываю...")

    with tempfile.TemporaryDirectory() as tmpdir:
        if message.document:
            file = await message.document.get_file()
            filename = message.document.file_name
        elif message.photo:
            file = await message.photo[-1].get_file()
            filename = "photo.jpg"
        else:
            await message.reply_text("❌ Неподдерживаемый тип файла")
            return

        filepath = f"{tmpdir}/{filename}"
        await file.download_to_drive(filepath)

        caption = message.caption or ""
        result = await agent.process_file(filepath, filename, caption)

        await send_result(message, result)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data.startswith("bankprofile:"):
        profile_name = data.split(":", 1)[1]

        if profile_name == "__new__":
            await query.edit_message_reply_markup(reply_markup=None)
            user_text = "Использовать новые реквизиты (введу их сейчас)"
        else:
            await query.edit_message_reply_markup(reply_markup=None)
            user_text = f"Использовать сохранённые реквизиты: {profile_name}"

        await query.message.reply_text("🤔 Думаю...")
        result = await agent.process_message(user_text)
        await send_result(query.message, result)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    await update.message.reply_text("🤔 Думаю...")

    result = await agent.process_message(user_text)

    await send_result(update.message, result)


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("memory", show_memory))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("del_instruction", del_instruction))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
