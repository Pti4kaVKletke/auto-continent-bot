import os
import logging
import asyncio
import random
import re
import time
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

AGENT_VERSION = os.environ.get("AGENT_VERSION", "v1")
if AGENT_VERSION == "v2":
    from agent_v2 import DocumentAgent
    logging.getLogger(__name__).info("Используется agent_v2")
else:
    from agent import DocumentAgent
    logging.getLogger(__name__).info("Используется agent_v1")

# Хелперы форматирования платежей (для локального рендера подменю оплат)
from agent import _parse_payments, _calc_total_amount, _fmt_money

import memory
from backup_service import BackupService

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

agent  = DocumentAgent()
backup = BackupService()


# ─── AWAITING-ФЛАГИ ──────────────────────────────────────────────────────────
# Единый список всех состояний "жду от пользователя ввода". Клик по любой
# кнопке или команда /menu, /start означают переход в новое состояние, а не
# продолжение прошлого ожидания → сбрасываем всё разом. Обработчик, который
# сам ставит awaiting-флаг, установит его ПОСЛЕ этого сброса — порядок верный.
AWAITING_FLAGS = (
    "awaiting_search",
    "awaiting_stats_dates",
    "awaiting_deal_date",
    "awaiting_scan_for_deal",
    "awaiting_scan_folder_id",
    "awaiting_scan_for_existing",
    "awaiting_edit_deal",
    "awaiting_payment_for_deal",
)

# Дополнительные "хвосты" — временные данные, привязанные к awaiting-состояниям
# как парные значения. Сбрасываем вместе с флагами.
# ⚠️  last_scan_* сюда НЕ входят: они читаются в самом handle_callback (ветка
# scan_route), сброс в начале колбэка их бы обнулил.
AWAITING_TAILS = (
    "pending_existing_filepath",
    "pending_existing_filename",
)


def _clear_awaiting_flags(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасывает все awaiting_* флаги и связанные с ними временные данные.
    Не трогает current_deal / new_deal_started (это не флаги ожидания ввода)."""
    for key in AWAITING_FLAGS + AWAITING_TAILS:
        context.user_data.pop(key, None)


# ─── ДЕТЕКТОР КОМАНД ─────────────────────────────────────────────────────────
# Защита от галлюцинации Haiku в v2 на свободных текстовых командах.
# Если пользователь пишет "добавь оплату N к сделке NNN" — LLM иногда просто
# сочиняет ответ вместо реального вызова add_payment. Здесь мы явно детектим
# намерение и через force_tool обязываем LLM вызвать конкретный инструмент.

_DEAL_NUM_RE = re.compile(r"\b\d{9}\b")
_PAY_ADD_RE  = re.compile(
    r"(?:добав\S*|запиш\S*|внес\S*|провед\S*)\s+.*?(?:оплат|плат|поступлен)"
    r"|(?:пришл\S*|поступил\S*)\s+.*?\d",
    re.IGNORECASE,
)
_PAY_DEL_RE  = re.compile(
    r"(?:удал\S*|убер\S*|отмен\S*)\s+.*?(?:оплат|плат|поступлен)",
    re.IGNORECASE,
)


def _detect_forced_tool(text: str) -> str:
    """Возвращает имя инструмента для форсирования или None.

    Работает только когда в тексте есть номер сделки (9 цифр) — иначе LLM
    сама решит нужен ли инструмент. Разделяем добавление и удаление платежа
    по глаголу.
    """
    if not _DEAL_NUM_RE.search(text):
        return None
    if _PAY_DEL_RE.search(text):
        return "remove_payment"
    if _PAY_ADD_RE.search(text):
        return "add_payment"
    return None

# ─── КОНТРОЛЬ ДОСТУПА ────────────────────────────────────────────────────────

ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "268470621"))

def _get_allowed_chat_ids() -> list[int]:
    """Список всех разрешённых chat_id — используется для рассылки
    административных уведомлений (алерты бэкапа и т.п.)."""
    return [
        int(x.strip())
        for x in os.environ.get("ALLOWED_CHAT_IDS", str(ALLOWED_CHAT_ID)).split(",")
        if x.strip().lstrip("-").isdigit()
    ]

async def check_access(update: Update) -> bool:
    allowed = set(_get_allowed_chat_ids())
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
        [
            InlineKeyboardButton("💾 Бэкапы",          callback_data="menu:backup"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    _clear_awaiting_flags(context)
    await update.message.reply_text(
        get_menu_text(),
        parse_mode="MarkdownV2",
        reply_markup=main_menu_keyboard(),
    )


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_awaiting_flags(context)
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


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /backup — быстрый доступ к подменю бэкапов."""
    if not await check_access(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    _clear_awaiting_flags(context)

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
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный автобэкап + ротация. Успех — тихо в логи, ошибка — алерт в чат."""
    logger.info("Запуск ежедневного бэкапа")
    result = await asyncio.to_thread(backup.create_backup)

    if result.get("success"):
        cleanup = await asyncio.to_thread(backup.cleanup_old_backups)
        # При успехе — молчим. Логов хватит. Не спамим Александру каждое утро.
        logger.info(
            f"Автобэкап OK: {result['file_name']} ({result['size_kb']} KB), "
            f"удалено старых: {cleanup.get('deleted', 0)}"
        )
        return

    # Ошибка — уведомляем всех разрешённых
    err = result.get("error", "неизвестно")
    logger.error(f"Автобэкап FAILED: {err}")
    msg = (
        "⚠️ *Ошибка ежедневного бэкапа*\n\n"
        f"`{err}`\n\n"
        "Проверь OAuth и доступ к Google Drive."
    )
    for chat_id in _get_allowed_chat_ids():
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Не удалось уведомить {chat_id} об ошибке бэкапа: {e}")


async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    memory.clear_history()
    memory.clear_pending_scans(chat_id)
    context.user_data.clear()
    await update.message.reply_text(
        "✅ История диалога очищена\n"
        "✅ Сохранённые сканы удалены\n"
        "✅ Текущий контекст сброшен",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
        ]])
    )


# ─── ОТПРАВКА РЕЗУЛЬТАТА ─────────────────────────────────────────────────────

async def send_result(message, result: dict, context=None, chat_id=None):
    """Отправляет файлы и текст результата."""
    # Пробрасываем данные ожидающего действия в user_data:
    # add_payment_impl при переоплате возвращает overpay_pending — bot.py
    # сохранит его для callback "payforce:confirm" (кнопка «Всё равно добавить»).
    if context is not None and result.get("overpay_pending"):
        context.user_data["overpay_pending"] = result["overpay_pending"]

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
            # Добавляем кнопку назад если её нет среди кнопок агента
            has_back = any("menu:back" in b["callback_data"] or "◀️" in b["text"]
                           for b in result["buttons"])
            if not has_back:
                keyboard.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text(result["text"], reply_markup=reply_markup)


# ─── ОБРАБОТКА ФАЙЛОВ ────────────────────────────────────────────────────────

async def _save_file_locally(message, chat_id: str) -> tuple[str, str] | None:
    """Скачивает файл, сохраняет в pending_scans, возвращает (filepath, filename)."""
    if message.document:
        file = await message.document.get_file()
        filename = message.document.file_name
    elif message.photo:
        file = await message.photo[-1].get_file()
        filename = "photo.jpg"
    else:
        return None

    scans_dir = Path("/data/pending_scans")
    scans_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = f"{int(time.time())}_{filename}"
    filepath = str(scans_dir / safe_filename)
    await file.download_to_drive(filepath)
    memory.add_pending_scan(chat_id, filepath, filename)
    logger.info(f"Скан сохранён: {filepath} (chat_id={chat_id})")
    return filepath, filename


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    message = update.message
    chat_id = str(update.effective_chat.id)

    saved = await _save_file_locally(message, chat_id)
    if not saved:
        await message.reply_text("❌ Неподдерживаемый тип файла")
        return
    filepath, filename = saved

    # ── Сценарий А: ждём скан для конкретной сделки (кнопка "📎 Загрузить скан") ──
    if context.user_data.get("awaiting_scan_for_deal"):
        contract_number = context.user_data.pop("awaiting_scan_for_deal")
        deal_folder_id  = context.user_data.pop("awaiting_scan_folder_id", None)

        if deal_folder_id:
            try:
                scans_folder_id = await agent.drive._get_or_create_folder("Сканы", deal_folder_id)
                link = await agent.drive.upload_file(filepath, filename, scans_folder_id)
                # Удаляем из pending_scans — файл уже загружен напрямую, не нужно повторять при create_contract
                memory.clear_pending_scans(chat_id)
                await message.reply_text(
                    f"✅ Скан загружен в папку сделки *{contract_number}*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ К сделке", callback_data=f"dealaction:{contract_number}:menu")
                    ]])
                )
            except Exception as e:
                logger.error(f"Ошибка загрузки скана для сделки {contract_number}: {e}", exc_info=True)
                await message.reply_text(f"⚠️ Не удалось загрузить скан: {e}")
        else:
            await message.reply_text(f"⚠️ Папка сделки {contract_number} не найдена на Drive.")
        return

    # ── Сценарий Г: файл без контекста — спрашиваем что делать ──
    caption = message.caption or ""

    has_history = len(memory.get_history(limit=3)) > 0
    buttons = [
        [InlineKeyboardButton("📄 Читать и начать новую сделку", callback_data="scan_route:new")],
    ]
    if has_history:
        buttons.append([InlineKeyboardButton("➕ Читать и добавить к текущей сделке", callback_data="scan_route:add")])
    buttons.append([InlineKeyboardButton("📂 Сохранить скан в существующую сделку", callback_data="scan_route:existing")])
    buttons.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])

    context.user_data["last_scan_filepath"] = filepath
    context.user_data["last_scan_filename"]  = filename
    context.user_data["last_scan_caption"]   = caption
    await message.reply_text("📎 Получила файл. Что с ним делать?", reply_markup=InlineKeyboardMarkup(buttons))



# ─── ОБРАБОТКА CALLBACK-КНОПОК ───────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        await query.message.reply_text("⛔ Доступ запрещён.")
        return

    # Любой клик по кнопке = переход в новое состояние. Сбрасываем все
    # висящие awaiting_* флаги. Если текущий обработчик сам ставит новый
    # awaiting-флаг (например, stats:custom или dealaction:X:edit) — он
    # установится ниже по коду, уже после сброса.
    _clear_awaiting_flags(context)

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
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                ]])
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
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                ]])
            )
            context.user_data["awaiting_search"] = True

        elif action == "active" or action.startswith("active:"):
            page = int(action.split(":")[1]) if ":" in action else 0
            per_page = 5

            await query.edit_message_text("🔄 Загружаю активные сделки...")
            # Пустой запрос вернёт все сделки, потом отфильтруем по статусу.
            # Раньше передавали "активна" как поиск по подстроке — статус
            # «ждём доплату» так не поймать, поэтому берём все и фильтруем.
            deals = await agent.sheets.find_deal("")
            deals = [
                d for d in deals
                if d.get("Статус", "").strip().lower() in ("активна", "ждём доплату")
            ]

            if not deals:
                await query.edit_message_text(
                    "Активных сделок нет.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                    ]])
                )
            else:
                total   = len(deals)
                total_pages = (total + per_page - 1) // per_page
                page    = max(0, min(page, total_pages - 1))
                chunk   = deals[page * per_page : (page + 1) * per_page]

                # Текст сообщения — 5 сделок подробно
                lines = [f"🔄 *Активные сделки* · {total} шт · стр. {page+1}/{total_pages}\n"]
                for d in chunk:
                    num      = d.get("Номер договора", "—")
                    car      = d.get("car_model", "—")
                    vin      = d.get("car_vin", "—")
                    # Фамилия И.О.
                    full     = d.get("buyer_initials") or d.get("buyer_name", "—")
                    lines.append(f"📄 `{num}` {full}\n    🚗 {car} · `...{vin[-6:]}`")

                # Кнопки — номер + Фамилия И.О.
                keyboard = []
                for d in chunk:
                    num   = d.get("Номер договора", "—")
                    init  = d.get("buyer_initials") or d.get("buyer_name", "—")
                    # Обрезаем до 25 символов чтобы влезло в кнопку
                    label = f"📄 {num} · {init}"[:32]
                    keyboard.append([InlineKeyboardButton(label, callback_data=f"dealaction:{num}:menu")])

                # Навигация
                nav = []
                if page > 0:
                    nav.append(InlineKeyboardButton("← Пред.", callback_data=f"menu:active:{page-1}"))
                if page < total_pages - 1:
                    nav.append(InlineKeyboardButton("След. →", callback_data=f"menu:active:{page+1}"))
                if nav:
                    keyboard.append(nav)
                keyboard.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])

                await query.edit_message_text(
                    "\n".join(lines),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        elif action == "stats":
            # Подменю выбора периода. Сам расчёт — в обработчике "stats:<period>".
            kb = [
                [
                    InlineKeyboardButton("📅 Сегодня", callback_data="stats:today"),
                    InlineKeyboardButton("📅 Вчера",   callback_data="stats:yesterday"),
                ],
                [
                    InlineKeyboardButton("📅 Неделя",  callback_data="stats:week"),
                    InlineKeyboardButton("📅 Месяц",   callback_data="stats:month"),
                ],
                [
                    InlineKeyboardButton("📅 Квартал", callback_data="stats:quarter"),
                    InlineKeyboardButton("📅 Год",     callback_data="stats:year"),
                ],
                [
                    InlineKeyboardButton("📊 Всё время", callback_data="stats:all"),
                ],
                [
                    InlineKeyboardButton("📅 Свой период", callback_data="stats:custom"),
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

        elif action == "back":
            context.user_data.pop("current_deal", None)
            await query.edit_message_text(
                get_menu_text(),
                parse_mode="MarkdownV2",
                reply_markup=main_menu_keyboard(),
            )
        return

    # ── Управление бэкапами ──────────────────────────────────────────────────
    if data.startswith("backup:"):
        sub = data.split(":", 1)[1]

        back_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К бэкапам", callback_data="menu:backup")],
            [InlineKeyboardButton("◀️ Меню",       callback_data="menu:back")],
        ])

        if sub == "create":
            await query.edit_message_text("💾 Создаю бэкап...")
            result = await asyncio.to_thread(backup.create_backup)
            if result.get("success"):
                text = (
                    "✅ *Бэкап создан*\n\n"
                    f"📄 `{result['file_name']}`\n"
                    f"💾 {result['size_kb']} KB\n"
                )
                if result.get("web_link"):
                    text += f"🔗 [Открыть в Drive]({result['web_link']})"
            else:
                text = f"❌ *Ошибка бэкапа*\n\n`{result.get('error', 'неизвестно')}`"
            await query.edit_message_text(text, parse_mode="Markdown",
                                          disable_web_page_preview=True,
                                          reply_markup=back_kb)

        elif sub == "list":
            files = await asyncio.to_thread(backup.list_backups, 10)
            if not files:
                text = "📋 *Бэкапов пока нет.*"
            else:
                lines = [f"📋 *Последние {len(files)} бэкапов:*\n"]
                for f in files:
                    link = f["web_link"]
                    if link:
                        lines.append(f"• [{f['created']}]({link}) · {f['size_kb']} KB")
                    else:
                        lines.append(f"• {f['created']} · {f['size_kb']} KB")
                text = "\n".join(lines)
            await query.edit_message_text(text, parse_mode="Markdown",
                                          disable_web_page_preview=True,
                                          reply_markup=back_kb)

        elif sub == "folder":
            link = await asyncio.to_thread(backup.get_folder_link)
            if link:
                text = f"📂 *Папка бэкапов в Drive:*\n\n{link}"
            else:
                text = "⚠️ Не удалось получить ссылку на папку."
            await query.edit_message_text(text, parse_mode="Markdown",
                                          disable_web_page_preview=True,
                                          reply_markup=back_kb)

        elif sub == "cleanup":
            await query.edit_message_text("🗑 Ищу старые бэкапы...")
            result = await asyncio.to_thread(backup.cleanup_old_backups)
            if result.get("success"):
                deleted = result.get("deleted", 0)
                if deleted:
                    text = f"✅ Удалено старых бэкапов: *{deleted}* (старше {result['kept_days']} дн.)"
                else:
                    text = f"✅ Нечего удалять — старше {result['kept_days']} дн. бэкапов нет."
            else:
                text = f"❌ Ошибка очистки: `{result.get('error', 'неизвестно')}`"
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_kb)

        return

    # ── Статистика по периоду ────────────────────────────────────────────────
    if data.startswith("stats:"):
        period = data.split(":", 1)[1]

        # Свой период — просим ввести диапазон дат
        if period == "custom":
            context.user_data["awaiting_stats_dates"] = True
            await query.edit_message_text(
                "📅 *Свой период*\n\n"
                "Введи диапазон дат в одном из форматов:\n"
                "• `01.06.2026 - 30.06.2026`\n"
                "• `01.06.2026 30.06.2026`\n"
                "• `01.06.2026` (одна дата — от неё до сегодня)",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Отмена", callback_data="menu:stats"),
                ]]),
            )
            return

        period_label = {
            "today":     "сегодня",
            "yesterday": "вчера",
            "week":      "неделю",
            "month":     "месяц",
            "quarter":   "квартал",
            "year":      "год",
            "all":       "всё время",
        }.get(period, period)
        await query.edit_message_text(f"📊 Считаю статистику за {period_label}...")

        # Вызываем инструмент напрямую через _execute_tool — избегаем LLM для
        # детерминированной операции (нет расхода токенов, нет риска галлюцинации,
        # мгновенный ответ).
        try:
            result = await agent._execute_tool("get_statistics", {"period": period})
        except Exception as e:
            logger.error(f"Ошибка вычисления статистики: {e}", exc_info=True)
            result = {"error": f"⚠️ Ошибка: {e}"}

        text = result.get("message") or result.get("error") or "Нет данных."
        kb = [
            [InlineKeyboardButton("◀️ К периодам", callback_data="menu:stats")],
            [InlineKeyboardButton("◀️ Меню",        callback_data="menu:back")],
        ]
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
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
            context.user_data["current_deal"] = num
            deal = await agent.sheets.get_deal(num)
            if not deal:
                await query.edit_message_text(f"❌ Сделка {num} не найдена.")
                return
            buyer  = deal.get("buyer_name", "—")
            seller = deal.get("seller_name", "—")
            car    = deal.get("car_model", "—")
            vin    = deal.get("car_vin", "—")
            price  = deal.get("car_price", "—")
            total_sum = deal.get("Сумма Договора", "")
            date   = deal.get("Дата договора", "—")
            folder = deal.get("Папка Drive", "")
            account_number = deal.get("account_number", "")
            bank_ben       = deal.get("bank_ben_line1", "")

            # Определяем название профиля реквизитов по номеру счёта и банку-корреспонденту
            profile_name = "—"
            deal_corr = deal.get("bank_corr_line1", "").strip().lower()
            for pname in memory.list_bank_profiles():
                p = memory.get_bank_profile(pname)
                if not p or not account_number:
                    continue
                # Совпадение по счёту
                if p.get("account_number") != account_number:
                    continue
                # Совпадение по банку-корреспонденту (для разных карт с одним счётом)
                p_corr = (p.get("bank_corr_line1", "") or "").strip().lower()
                if p_corr == deal_corr:
                    profile_name = pname
                    break
            # Если по паре не нашли — берём первый по номеру счёта (фолбэк)
            if profile_name == "—":
                for pname in memory.list_bank_profiles():
                    p = memory.get_bank_profile(pname)
                    if p and p.get("account_number") == account_number and account_number:
                        profile_name = pname
                        break

            # Для старых сделок без ссылки — строим её по номеру договора
            if not folder:
                try:
                    folder_id = await agent.drive.get_or_create_deal_folder(num)
                    folder = f"https://drive.google.com/drive/folders/{folder_id}"
                except Exception:
                    pass

            # Краткое название банка для отображения
            bank_short = bank_ben[:40] + "..." if len(bank_ben) > 43 else bank_ben
            text = (
                f"📄 *Сделка {num}* от {date}\n\n"
                f"👤 {buyer}\n"
                f"👤 {seller}\n"
                f"🚗 {car} · VIN `{vin}`\n"
                f"💰 Цена авто: {price} руб."
            )
            if total_sum:
                text += f"\n💵 Итого к оплате: *{total_sum}* руб."
            text += f"\n🏦 {profile_name}"
            if bank_short:
                text += f"\n   _{bank_short}_"
            if account_number:
                text += f"\n   Счёт: `{account_number}`"
            keyboard = [
                [InlineKeyboardButton("📋 Создать документы", callback_data=f"dealaction:{num}:docs")],
                [InlineKeyboardButton("✏️ Изменить данные",   callback_data=f"dealaction:{num}:edit")],
                [InlineKeyboardButton("📎 Загрузить скан",    callback_data=f"dealaction:{num}:scan"),
                 InlineKeyboardButton("🗂 Сканы",             callback_data=f"dealaction:{num}:scans")],
                [InlineKeyboardButton("💳 Оплаты",            callback_data=f"dealaction:{num}:payments")],
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

        elif action == "scans":
            deal = await agent.sheets.get_deal(num)
            folder_link = deal.get("Папка Drive", "") if deal else ""
            folder_id = folder_link.split("/folders/")[-1].split("?")[0] if "/folders/" in folder_link else ""

            # Если ссылки нет — ищем/создаём папку по номеру договора
            if not folder_id:
                try:
                    folder_id = await agent.drive.get_or_create_deal_folder(num)
                except Exception as e:
                    logger.error(f"Не удалось найти папку для {num}: {e}", exc_info=True)

            if not folder_id:
                await query.edit_message_text(
                    f"❌ Папка сделки {num} не найдена на Drive.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Назад", callback_data=f"dealaction:{num}:menu")
                    ]])
                )
                return

            # Ищем папку Сканы внутри папки сделки
            def _list_scans():
                svc = agent.drive.service
                # Находим папку Сканы
                q = (f"name='Сканы' and mimeType='application/vnd.google-apps.folder' "
                     f"and '{folder_id}' in parents and trashed=false")
                res = svc.files().list(q=q, fields="files(id,name)").execute()
                scans_folders = res.get("files", [])
                if not scans_folders:
                    return None, []
                scans_id = scans_folders[0]["id"]
                # Список файлов в папке Сканы
                q2 = f"'{scans_id}' in parents and trashed=false"
                res2 = svc.files().list(
                    q=q2,
                    fields="files(id,name,mimeType,size,webViewLink,createdTime)",
                    orderBy="createdTime desc"
                ).execute()
                return scans_id, res2.get("files", [])

            try:
                scans_id, files = await asyncio.to_thread(_list_scans)
            except Exception as e:
                logger.error(f"Ошибка чтения сканов для {num}: {e}", exc_info=True)
                await query.edit_message_text(
                    f"⚠️ Не удалось прочитать папку сканов: {e}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Назад", callback_data=f"dealaction:{num}:menu")
                    ]])
                )
                return

            if scans_id is None:
                text = f"🗂 *Сканы сделки {num}*\n\nПапка «Сканы» не найдена."
            elif not files:
                text = f"🗂 *Сканы сделки {num}*\n\nПапка пуста — сканов нет."
            else:
                lines = [f"🗂 *Сканы сделки {num}* · {len(files)} файл(ов)\n"]
                for f in files:
                    name = f.get("name", "—")
                    size = f.get("size", "")
                    size_str = f" · {int(size)//1024} КБ" if size else ""
                    link = f.get("webViewLink", "")
                    if link:
                        lines.append(f"📄 [{name}]({link}){size_str}")
                    else:
                        lines.append(f"📄 {name}{size_str}")
                text = "\n".join(lines)

            scans_folder_url = f"https://drive.google.com/drive/folders/{scans_id}" if scans_id else ""
            kb = []
            if scans_folder_url:
                kb.append([InlineKeyboardButton("📁 Открыть папку Сканы", url=scans_folder_url)])
            kb.append([InlineKeyboardButton("📎 Загрузить скан", callback_data=f"dealaction:{num}:scan")])
            kb.append([InlineKeyboardButton("◀️ Назад",          callback_data=f"dealaction:{num}:menu")])

            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
                disable_web_page_preview=True,
            )

        elif action == "scan":
            # Получаем folder_id для этой сделки
            deal = await agent.sheets.get_deal(num)
            folder_link = deal.get("Папка Drive", "") if deal else ""
            # Извлекаем folder_id из ссылки Drive
            folder_id = folder_link.split("/folders/")[-1].split("?")[0] if "/folders/" in folder_link else ""
            if not folder_id:
                # Создаём папку если нет ссылки
                folder_id = await agent.drive.get_or_create_deal_folder(num)
            context.user_data["awaiting_scan_for_deal"]  = num
            context.user_data["awaiting_scan_folder_id"] = folder_id
            await query.edit_message_text(
                f"📎 Отправь скан для сделки *{num}*\n\nЖду файл или фото...",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Отмена", callback_data=f"dealaction:{num}:menu")
                ]])
            )

        elif action == "edit":
            await query.edit_message_text(
                f"✏️ *Изменить данные сделки {num}*\n\n"
                "Напиши что именно нужно изменить. Например:\n"
                "• `реквизиты на ВТБ`\n"
                "• `имя покупателя Иванов Иван Иванович`\n"
                "• `цену на 4500000`\n"
                "• `статус на завершена`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Отмена", callback_data=f"dealaction:{num}:menu")
                ]])
            )
            context.user_data["awaiting_edit_deal"] = num

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

        # ── ОПЛАТЫ ────────────────────────────────────────────────────────
        elif action == "payments":
            deal = await agent.sheets.get_deal(num)
            if not deal:
                await query.edit_message_text(f"❌ Сделка {num} не найдена.")
                return

            payments = _parse_payments(deal.get("Платежи", ""))
            total    = _calc_total_amount(deal)
            received = sum(p["amount"] for p in payments)
            remainder = total - received
            currency = (deal.get("currency") or "руб").strip()

            lines = [f"💳 *Оплаты по сделке {num}*", ""]
            if not payments:
                lines.append("_Поступлений ещё не было_")
            else:
                for i, p in enumerate(payments, 1):
                    lines.append(f"  {i}. {_fmt_money(p['amount'])} {currency}  от  {p['date']}")
            lines += [
                "",
                f"💰 Сумма договора: *{_fmt_money(total)}* {currency}",
                f"📥 Получено: *{_fmt_money(received)}* {currency}",
                f"⏳ Остаток: *{_fmt_money(remainder)}* {currency}",
            ]

            kb = [[InlineKeyboardButton("➕ Добавить оплату", callback_data=f"dealaction:{num}:pay_add")]]
            if payments:
                kb.append([InlineKeyboardButton("❌ Удалить оплату", callback_data=f"dealaction:{num}:pay_del")])
            kb.append([InlineKeyboardButton("◀️ К сделке", callback_data=f"dealaction:{num}:menu")])

            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
            )

        elif action == "pay_add":
            context.user_data["awaiting_payment_for_deal"] = num
            await query.edit_message_text(
                f"➕ *Добавить оплату по сделке {num}*\n\n"
                "Напиши сумму и дату поступления одним сообщением.\n\n"
                "Примеры:\n"
                "• `500000 сегодня`\n"
                "• `500000 01.07`\n"
                "• `1500000 02.07.2026`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Отмена", callback_data=f"dealaction:{num}:payments")
                ]]),
            )

        elif action == "pay_del":
            deal = await agent.sheets.get_deal(num)
            payments = _parse_payments(deal.get("Платежи", "")) if deal else []
            if not payments:
                await query.answer("Нет платежей для удаления", show_alert=True)
                return

            currency = (deal.get("currency") or "руб").strip()
            kb = []
            for i, p in enumerate(payments, 1):
                label = f"❌ №{i}: {_fmt_money(p['amount'])} {currency} от {p['date']}"
                kb.append([InlineKeyboardButton(label, callback_data=f"payrm:{num}:{i}")])
            kb.append([InlineKeyboardButton("◀️ Назад", callback_data=f"dealaction:{num}:payments")])

            await query.edit_message_text(
                f"❌ *Удалить оплату по сделке {num}*\n\nВыбери платёж:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
            )

    # ── Удаление конкретной оплаты по индексу ────────────────────────────────
    elif data.startswith("payrm:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            contract_number = parts[1]
            try:
                index = int(parts[2])
            except ValueError:
                await query.answer("Ошибка", show_alert=True)
                return

            await query.edit_message_text(f"⏳ Удаляю платёж №{index} из сделки {contract_number}...")
            result = await typing_while(
                update.effective_chat.id, context,
                agent.process_message(
                    f"удали платёж №{index} из сделки {contract_number}",
                    chat_id=str(update.effective_chat.id),
                    force_tool="remove_payment",   # защита от галлюцинации
                ),
            )
            await send_result(query.message, result, context=context)

    # ── Подтверждение переоплаты («Всё равно добавить» после блокировки) ─────
    elif data == "payforce:confirm":
        pending = context.user_data.pop("overpay_pending", None)
        if not pending:
            await query.answer("Данные ожидания утеряны, попробуйте добавить оплату заново", show_alert=True)
            return

        contract_number = pending["contract_number"]
        await query.edit_message_text(
            f"⏳ Добавляю платёж с переоплатой в сделку {contract_number}..."
        )
        # Вызываем метод напрямую с force=True — обход блокировки переоплаты.
        result = await typing_while(
            update.effective_chat.id, context,
            agent.add_payment_impl(
                contract_number=contract_number,
                amount_in=pending["amount"],
                date=pending["date"],
                force=True,
            ),
        )
        await send_result(query.message, result, context=context)

    # ── Смена реквизитов в существующей сделке ───────────────────────────────
    elif data.startswith("editbank:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            contract_number = parts[1]
            profile_name    = parts[2]

            if profile_name == "__new__":
                await query.edit_message_reply_markup(reply_markup=None)
                context.user_data["awaiting_edit_deal"] = contract_number
                await query.message.reply_text(
                    "Введи новые реквизиты текстом — я обновлю их в сделке."
                )
                return

            # Берём реквизиты из профиля и обновляем сделку
            profile = memory.get_bank_profile(profile_name)
            if not profile:
                await query.answer("Профиль не найден", show_alert=True)
                return

            await query.edit_message_reply_markup(reply_markup=None)
            ok = await agent.sheets.update_deal(contract_number, profile)
            if ok:
                await query.message.reply_text(
                    f"✅ Реквизиты сделки *{contract_number}* обновлены на *{profile_name}*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ К сделке", callback_data=f"dealaction:{contract_number}:menu")
                    ]])
                )
            else:
                await query.message.reply_text(f"❌ Не удалось обновить сделку {contract_number}")

    # ── Маршрутизация скана (существующая / новая сделка) ────────────────────
    elif data.startswith("scan_route:"):
        route    = data.split(":", 1)[1]
        filepath = context.user_data.pop("last_scan_filepath", None)
        filename = context.user_data.pop("last_scan_filename", "file")
        caption  = context.user_data.pop("last_scan_caption", "")

        if route == "new":
            # Новая сделка — читаем документ с нуля
            await query.edit_message_text("📥 Читаю документ...")
            result = await typing_while(
                update.effective_chat.id, context,
                agent.process_file(filepath, filename, caption, chat_id=str(update.effective_chat.id))
            )
            await send_result(query.message, result)

        elif route == "add":
            # Добавляем данные к текущей сделке — читаем документ в контексте истории
            await query.edit_message_text("📥 Читаю документ и добавляю данные...")
            result = await typing_while(
                update.effective_chat.id, context,
                agent.process_file(
                    filepath, filename,
                    caption or "Извлеки данные из документа и дополни уже собранные данные для сделки.",
                    chat_id=str(update.effective_chat.id)
                )
            )
            await send_result(query.message, result)

        elif route == "existing":
            # Сохраняем скан в папку существующей сделки
            context.user_data["awaiting_scan_for_existing"] = True
            context.user_data["pending_existing_filepath"]  = filepath
            context.user_data["pending_existing_filename"]  = filename
            await query.edit_message_text(
                "Укажи номер сделки (например: `280626001`):",
                parse_mode="Markdown",
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

    # ── Ожидание ввода суммы+даты для новой оплаты ──────────────────────────
    if context.user_data.get("awaiting_payment_for_deal"):
        contract_number = context.user_data.pop("awaiting_payment_for_deal")
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(
                f"добавь платёж по сделке {contract_number}: {user_text}",
                chat_id=chat_id,
                force_tool="add_payment",   # защита от галлюцинации: LLM обязана вызвать инструмент
            ),
        )
        await send_result(update.message, result, context=context, chat_id=chat_id)
        return

    if context.user_data.get("awaiting_edit_deal"):
        contract_number = context.user_data.get("awaiting_edit_deal")

        # Если пользователь хочет сменить реквизиты — показываем кнопки профилей
        if any(word in user_text.lower() for word in ["реквизит", "банк", "счёт", "счет"]):
            context.user_data.pop("awaiting_edit_deal")
            context.user_data["edit_deal_bank_number"] = contract_number
            profiles = memory.list_bank_profiles()
            buttons = [{"text": name, "callback_data": f"editbank:{contract_number}:{name}"} for name in profiles]
            buttons.append({"text": "🆕 Новые реквизиты", "callback_data": f"editbank:{contract_number}:__new__"})
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])]
                for b in buttons
            ])
            await update.message.reply_text(
                f"Какие реквизиты использовать для сделки *{contract_number}*?",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            return

        # Иначе передаём агенту как обычно
        context.user_data.pop("awaiting_edit_deal")
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(
                f"Обнови данные сделки {contract_number}: {user_text}",
                chat_id=chat_id
            )
        )
        await send_result(update.message, result, context=context, chat_id=chat_id)
        return

    if context.user_data.get("awaiting_scan_for_existing"):
        context.user_data.pop("awaiting_scan_for_existing")
        contract_number = user_text.strip()
        filepath = context.user_data.pop("pending_existing_filepath", None)
        filename  = context.user_data.pop("pending_existing_filename", "file")

        if not filepath or not Path(filepath).exists():
            await update.message.reply_text("⚠️ Файл не найден, попробуй загрузить снова.")
            return

        try:
            deal_folder_id  = await agent.drive.get_or_create_deal_folder(contract_number)
            scans_folder_id = await agent.drive._get_or_create_folder("Сканы", deal_folder_id)
            await typing_while(
                update.effective_chat.id, context,
                agent.drive.upload_file(filepath, filename, scans_folder_id)
            )
            await update.message.reply_text(
                f"✅ Скан загружен в папку сделки *{contract_number}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
                ]])
            )
        except Exception as e:
            logger.error(f"Ошибка загрузки скана: {e}", exc_info=True)
            await update.message.reply_text(f"⚠️ Ошибка загрузки: {e}")
        return

    if context.user_data.get("awaiting_stats_dates"):
        context.user_data["awaiting_stats_dates"] = False
        raw = user_text.strip()

        # Достаём все даты в формате ДД.ММ.ГГГГ из строки
        import re as _re
        matches = _re.findall(r"\d{1,2}\.\d{1,2}\.\d{4}", raw)

        if not matches:
            await update.message.reply_text(
                "⚠️ Не нашёл ни одной даты. Ожидаю формат ДД.ММ.ГГГГ, например "
                "`01.06.2026 - 30.06.2026`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К периодам", callback_data="menu:stats"),
                ]]),
            )
            return

        date_from = matches[0]
        date_to   = matches[1] if len(matches) >= 2 else ""  # пусто → до сегодня (см. _resolve_period)

        # Валидация: дата_от не должна быть позже даты_до
        from datetime import datetime as _dt
        try:
            df = _dt.strptime(date_from, "%d.%m.%Y").date()
            dt_end = _dt.strptime(date_to, "%d.%m.%Y").date() if date_to else None
            if dt_end and df > dt_end:
                await update.message.reply_text(
                    f"⚠️ Начало периода `{date_from}` позже конца `{date_to}`. "
                    "Проверь порядок дат.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ К периодам", callback_data="menu:stats"),
                    ]]),
                )
                return
        except ValueError as e:
            await update.message.reply_text(
                f"⚠️ Не удалось разобрать дату: {e}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К периодам", callback_data="menu:stats"),
                ]]),
            )
            return

        loading = await update.message.reply_text(
            f"📊 Считаю статистику за {date_from} — {date_to or 'сегодня'}..."
        )
        try:
            result = await agent._execute_tool("get_statistics", {
                "period":    "custom",
                "date_from": date_from,
                "date_to":   date_to,
            })
        except Exception as e:
            logger.error(f"Ошибка вычисления статистики (custom): {e}", exc_info=True)
            result = {"error": f"⚠️ Ошибка: {e}"}

        text = result.get("message") or result.get("error") or "Нет данных."
        kb = [
            [InlineKeyboardButton("◀️ К периодам", callback_data="menu:stats")],
            [InlineKeyboardButton("◀️ Меню",        callback_data="menu:back")],
        ]
        try:
            await loading.edit_text(text, parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await update.message.reply_text(text, parse_mode="Markdown",
                                            reply_markup=InlineKeyboardMarkup(kb))
        return

    if context.user_data.get("awaiting_deal_date"):
        context.user_data["awaiting_deal_date"] = False
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(f"Дата договора: {user_text.strip()}", chat_id=chat_id)
        )
        await send_result(update.message, result, context=context, chat_id=str(update.effective_chat.id))
        return

    if context.user_data.get("awaiting_search"):
        context.user_data["awaiting_search"] = False
        deals = await agent.sheets.find_deal(user_text.strip())
        if not deals:
            await update.message.reply_text(
                f"❌ По запросу «{user_text}» ничего не найдено.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔍 Искать снова", callback_data="menu:find_deal"),
                    InlineKeyboardButton("◀️ Меню",         callback_data="menu:back"),
                ]])
            )
        else:
            lines = [f"🔍 *Найдено: {len(deals)}*\n"]
            keyboard = []
            for d in deals[:10]:
                num    = d.get("Номер договора", "—")
                status = d.get("Статус", "—")
                car    = d.get("car_model", "—")
                vin    = d.get("car_vin", "—")
                init   = d.get("buyer_initials") or d.get("buyer_name", "—")
                date   = d.get("Дата договора", "")
                lines.append(f"📄 `{num}` {init}\n    🚗 {car} · `...{vin[-6:]}` · {date} [{status}]")
                label = f"📄 {num} · {init}"[:32]
                keyboard.append([InlineKeyboardButton(label, callback_data=f"dealaction:{num}:menu")])
            if len(deals) > 10:
                lines.append(f"\n_...и ещё {len(deals)-10}. Уточни запрос._")
            keyboard.append([
                InlineKeyboardButton("🔍 Искать снова", callback_data="menu:find_deal"),
                InlineKeyboardButton("◀️ Меню",         callback_data="menu:back"),
            ])
            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return

    # Если есть открытая сделка и пользователь задал короткий вопрос — добавляем контекст
    current_deal = context.user_data.get("current_deal")
    message_to_agent = user_text
    if current_deal and len(user_text) < 200:
        # Если в тексте уже есть номер сделки (6+ цифр) — оставляем как есть
        if not re.search(r"\d{6,}", user_text):
            message_to_agent = f"{user_text} (контекст: сделка {current_deal})"

    # Детекция команд платежей → форсим tool_choice, чтобы LLM не галлюцинировала
    forced = _detect_forced_tool(message_to_agent)
    if forced:
        logger.info(f"[detector] Обнаружена команда → force_tool={forced}")

    result = await typing_while(
        update.effective_chat.id, context,
        agent.process_message(message_to_agent, chat_id=chat_id, force_tool=forced)
    )
    await send_result(update.message, result, context=context, chat_id=str(update.effective_chat.id))


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
    app.add_handler(CommandHandler("backup",          cmd_backup))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    # ── Ежедневный автобэкап в 03:00 по Бишкеку ──────────────────────────
    if app.job_queue is None:
        logger.warning(
            "JobQueue недоступен (не установлен python-telegram-bot[job-queue]) — "
            "автобэкап отключён. Ручной бэкап через /backup работает."
        )
    else:
        try:
            from datetime import time as _dt_time
            try:
                from zoneinfo import ZoneInfo
                bishkek_tz = ZoneInfo("Asia/Bishkek")
            except Exception:
                # Фолбэк: контейнер без tzdata → фиксированный UTC+6
                from datetime import timezone as _tz, timedelta as _td
                bishkek_tz = _tz(_td(hours=6))

            hour   = int(os.environ.get("BACKUP_HOUR",   "3"))
            minute = int(os.environ.get("BACKUP_MINUTE", "0"))
            app.job_queue.run_daily(
                daily_backup_job,
                time=_dt_time(hour=hour, minute=minute, tzinfo=bishkek_tz),
                name="daily_backup",
            )
            logger.info(f"Автобэкап запланирован на {hour:02d}:{minute:02d} Asia/Bishkek")
        except Exception as e:
            logger.error(f"Не удалось запланировать автобэкап: {e}", exc_info=True)

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
