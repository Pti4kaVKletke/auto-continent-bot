import os
import time

# ── Часовой пояс процесса: Бишкек (UTC+6, без перехода на летнее время) ──
# Контейнер Railway живёт в UTC, а datetime.now() по всему коду даёт «сегодня»
# для документов и промпта. Без этого с 00:00 до 06:00 по Бишкеку ставилась
# вчерашняя дата. POSIX-строка "<+06>-6" не требует tzdata в образе.
# Переопределить можно переменной BOT_TZ (например, "Asia/Bishkek" при наличии tzdata).
os.environ["TZ"] = os.environ.get("BOT_TZ", "<+06>-6")
if hasattr(time, "tzset"):
    time.tzset()

import asyncio
import re
from pathlib import Path
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from agent import _money_str, SCAN_TYPES
import memory
import bank_requisites as br
import bank_ui
import company_ui
import salon
import salon_ui
import sign_ui
import settings_service

# Общие объекты и помощники — в bot_core.py; кнопки — в cb_*.py (25.09.2026)
from bot_core import (
    _DEAL_NUM_RE,
    _PAY_ADD_RE,
    _PAY_DEL_RE,
    _REGEN_DEFAULT_BATCH,
    _build_deals_view,
    _clear_awaiting_flags,
    _get_allowed_chat_ids,
    _make_card_sender,
    _safe_reply,
    agent,
    apply_doc_field,
    ask_dkp_date,
    backup,
    check_access,
    get_menu_text,
    logger,
    main_menu_keyboard,
    send_result,
    typing_while,
)
import cb_menu
import cb_service
import cb_deals
import cb_deal


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


async def cmd_regen_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /regen_docs [N|все] — пересборка комплекта документов по
    сделкам, у которых папка на Drive пуста (старые файлы Илья убрал в архив
    вручную после замены шаблонов). Сначала — сухой прогон со списком
    кандидатов и подтверждением, сама пересборка запускается только по кнопке.

    Без аргумента берёт первые _REGEN_DEFAULT_BATCH кандидатов, а не всё
    сразу — при сбое посреди прогона (сеть, квота Drive, рестарт на Railway)
    проще понять, что уже сделано, и не гнать сотни сделок одним махом.
    /regen_docs 30 — первые 30. /regen_docs все — без ограничения.

    Упавший на середине прогон не теряет прогресс: то, что уже собрано,
    лежит в Drive и в журнале, а следующий /regen_docs сам не увидит эти
    сделки среди кандидатов (их папка уже не пустая) — можно просто
    запустить снова, лишнего не пересоберёт.
    """
    if not await check_access(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    if context.bot_data.get("regen_docs_running"):
        await update.message.reply_text("⏳ Пересборка уже идёт, дождитесь отчёта.")
        return

    limit = _REGEN_DEFAULT_BATCH
    if context.args:
        raw = context.args[0].strip().lower()
        if raw in ("все", "всё", "all"):
            limit = None
        else:
            try:
                limit = max(1, int(raw))
            except ValueError:
                await update.message.reply_text(
                    "Не понял число. Примеры: /regen_docs, /regen_docs 30, /regen_docs все"
                )
                return

    await update.message.reply_text("🔍 Проверяю папки сделок на Drive...")
    info = await typing_while(update.effective_chat.id, context,
                              agent.find_deals_missing_docs())

    all_candidates = info["candidates"]
    resolved       = info.get("resolved_by_lookup") or {}
    total_deals    = (len(all_candidates) + len(info["has_docs"])
                      + len(info["no_folder"]) + len(info["cancelled"]))

    if not all_candidates:
        lines = [
            "✅ Пустых папок не найдено — пересобирать нечего.",
            f"Всего сделок в журнале: {total_deals}",
            f"Сделок с документами: {len(info['has_docs'])}",
        ]
        if info["cancelled"]:
            lines.append(f"Отменённые — не проверяли: {len(info['cancelled'])}")
        if resolved:
            lines.append(
                f"Папку пришлось восстановить по номеру (в журнале не было "
                f"ссылки): {len(resolved)} — проверены наравне с остальными."
            )
        if info["no_folder"]:
            sample = ", ".join(info["no_folder"][:10])
            more = "…" if len(info["no_folder"]) > 10 else ""
            lines.append(
                f"⚠️ Папку не нашли и не восстановили (не проверяли вообще): "
                f"{len(info['no_folder'])} — {sample}{more}"
            )
        await update.message.reply_text("\n".join(lines))
        return

    run_candidates = all_candidates if limit is None else all_candidates[:limit]
    remaining = len(all_candidates) - len(run_candidates)

    lines = [
        "🧾 *Пересборка документов*\n",
        f"Всего сделок в журнале: {total_deals}",
        f"Пустых папок всего (кандидаты): *{len(all_candidates)}*",
    ]
    if remaining > 0:
        lines.append(
            f"В этом прогоне: *{len(run_candidates)}* — лимит по умолчанию "
            f"{_REGEN_DEFAULT_BATCH}. Чтобы обработать сразу все — `/regen_docs все`, "
            "другое число — `/regen_docs <число>`."
        )
    lines.append(f"С документами — не тронем: {len(info['has_docs'])}")
    if info["cancelled"]:
        lines.append(f"Отменённые сделки — не трогаем: {len(info['cancelled'])}")
    if resolved:
        lines.append(
            f"Папку восстановили по номеру (в журнале не было ссылки): "
            f"{len(resolved)} — проверены наравне с остальными."
        )
    if info["no_folder"]:
        sample = ", ".join(info["no_folder"][:10])
        more = "…" if len(info["no_folder"]) > 10 else ""
        lines.append(
            f"⚠️ Папку не нашли и не восстановили (не проверяли вообще): "
            f"{len(info['no_folder'])} — {sample}{more}"
        )

    sample = ", ".join(run_candidates[:15])
    more = "…" if len(run_candidates) > 15 else ""
    lines.append(f"\nНомера в этом прогоне: {sample}{more}")
    lines.append(
        "\nПо каждой: агентский договор + ДКП + счёт, затем расписка/акт/отчёт "
        "— если хватает данных (дата расчёта, фактический курс). Чего не хватит "
        "— попадёт в отчёт, ничего спрашивать не будет."
    )
    if remaining > 0:
        lines.append(f"\nПосле этого прогона останется ещё {remaining} — обработаете следующим `/regen_docs`.")

    context.user_data["regen_docs_candidates"] = run_candidates
    kb = [
        [InlineKeyboardButton(f"▶️ Запустить по {len(run_candidates)}", callback_data="regendocs:run")],
        [InlineKeyboardButton("❌ Отмена", callback_data="regendocs:cancel")],
    ]
    await _safe_reply(update.message, "\n".join(lines), parse_mode="Markdown",
                      reply_markup=InlineKeyboardMarkup(kb))

async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный автобэкап + ротация. Успех — тихо в логи, ошибка — алерт в чат."""
    logger.info("Запуск ежедневного бэкапа")
    result = await asyncio.to_thread(backup.create_backup)

    # Копия SQLite (компании, банковские профили, инструкции, настройки).
    # Отдельно от журнала: ошибка здесь не должна глушить бэкап таблицы.
    db_result = await asyncio.to_thread(backup.create_db_backup)
    if not db_result.get("success"):
        db_err = db_result.get("error", "неизвестно")
        logger.error(f"Бэкап agent.db FAILED: {db_err}")
        for chat_id in _get_allowed_chat_ids():
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Не удалось сделать бэкап базы бота (agent.db): {db_err}",
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить {chat_id} об ошибке бэкапа БД: {e}")
    else:
        logger.info(f"Бэкап БД OK: {db_result['file_name']} ({db_result['size_kb']} KB)")

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
    memory.clear_history(chat_id)
    memory.clear_pending_scans(chat_id)
    salon.clear_pending(chat_id)
    context.user_data.clear()
    await update.message.reply_text(
        "✅ История диалога очищена\n"
        "✅ Сохранённые сканы удалены\n"
        "✅ Текущий контекст сброшен",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Меню", callback_data="menu:back")
        ]])
    )


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

    # ── Сценарий П: ждём документ на подпись (кнопка "✍️ Подписать") ──
    if context.user_data.pop("awaiting_doc_to_sign", None):
        await sign_ui.start(message, context, filepath, filename,
                            agent.builder, getter=memory.get_setting)
        return

    # ── Сценарий С: ждём карточку нового салона (меню «🏬 Салоны») ──
    if await salon_ui.handle_file(update, context, agent, filepath, filename):
        return

    # ── Сценарий А: ждём скан для конкретной сделки (кнопка "📎 Загрузить скан") ──
    if context.user_data.get("awaiting_scan_for_deal"):
        contract_number = context.user_data.pop("awaiting_scan_for_deal")
        deal_folder_id  = context.user_data.pop("awaiting_scan_folder_id", None)

        if not deal_folder_id:
            await message.reply_text(f"⚠️ Папка сделки {contract_number} не найдена на Drive.")
            return

        # Файл не грузим сразу: сначала спрашиваем, что это за документ.
        # Тип уходит в имя файла — по нему потом читается статус комплекта
        # и видно содержимое папки без открывания файлов.
        context.user_data["pending_scan"] = {
            "num":       contract_number,
            "folder_id": deal_folder_id,
            "filepath":  filepath,
            "filename":  filename,
        }
        kb = [[InlineKeyboardButton(label, callback_data=f"scantype:{code}")]
              for code, label, _, _ in SCAN_TYPES]
        kb.append([InlineKeyboardButton("◀️ Отмена",
                                        callback_data=f"dealaction:{contract_number}:menu")])
        await message.reply_text(
            f"📎 Файл получен. Что это за документ по сделке *{contract_number}*?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    # ── Сценарий Н: ждём документы новой сделки (кнопка "📄 Новая сделка") ──
    intake = context.user_data.get("awaiting_new_deal_docs")
    if intake:
        caption = message.caption or ""
        if intake > 1:
            caption = caption or "Извлеки данные из документа и дополни уже собранные данные для сделки."
        context.user_data["awaiting_new_deal_docs"] = intake + 1
        status = await message.reply_text(
            "📥 Читаю документ..." if intake == 1 else "📥 Читаю документ и добавляю данные...")
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_file(filepath, filename, caption, chat_id=chat_id)
        )
        try:
            await status.delete()
        except Exception:
            pass
        await send_result(message, result, context=context, chat_id=chat_id)
        return

    # ── Сценарий Г: файл без контекста — спрашиваем что делать ──
    caption = message.caption or ""

    has_history = len(memory.get_history(limit=3, chat_id=chat_id)) > 0
    buttons = [
        [InlineKeyboardButton("📄 Читать и начать новую сделку", callback_data="scan_route:new")],
    ]
    if has_history:
        buttons.append([InlineKeyboardButton("➕ Читать и добавить к текущей сделке", callback_data="scan_route:add")])
    buttons.append([InlineKeyboardButton("📂 Сохранить скан в существующую сделку", callback_data="scan_route:existing")])
    buttons.append([InlineKeyboardButton("✍️ Подписать документ", callback_data="scan_route:sign")])
    buttons.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])

    context.user_data["last_scan_filepath"] = filepath
    context.user_data["last_scan_filename"]  = filename
    context.user_data["last_scan_caption"]   = caption
    await message.reply_text("📎 Получила файл. Что с ним делать?", reply_markup=InlineKeyboardMarkup(buttons))


# ─── МАРШРУТЫ КНОПОК ──────────────────────────────────────────────────────────
# Префикс callback_data → обработчик. Префиксы не пересекаются, поэтому порядок
# не важен. Кнопки bp:/co:/sign: разбираются раньше, в самом handle_callback.
CALLBACK_ROUTES = [
    ("prefix", "menu:", cb_menu.on_menu),
    ("prefix", "backup:", cb_service.on_backup),
    ("prefix", "settings:", cb_service.on_settings),
    ("prefix", "copy:", cb_deals.on_copy),
    ("prefix", "regendocs:", cb_service.on_regendocs),
    ("prefix", "rescan:", cb_service.on_rescan),
    ("prefix", "deals:", cb_deals.on_deals),
    ("prefix", "stats:", cb_deals.on_stats),
    ("prefix", "deal_date:", cb_deal.on_deal_date),
    ("prefix", "dkp_date:", cb_deal.on_dkp_date),
    ("prefix", "scantype:", cb_deal.on_scantype),
    ("prefix", "docfield:", cb_deal.on_docfield),
    ("prefix", "docmenu:", cb_deal.on_docmenu),
    ("prefix", "dealaction:", cb_deal.on_dealaction),
    ("prefix", "payrm:", cb_deal.on_payrm),
    ("eq", "payforce:confirm", cb_deal.on_payforce_confirm),
    ("prefix", "editbank:", cb_deal.on_editbank),
    ("prefix", "scan_route:", cb_deal.on_scan_route),
    ("prefix", "bankprofile:", cb_deal.on_bankprofile),
]


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

    # Ожидание поля для документа живёт в отдельном ключе (его значение нужно
    # самой ветке docfield:), поэтому сбрасываем его здесь и вручную: клик по
    # любой ДРУГОЙ кнопке означает, что пользователь ушёл от этого вопроса и
    # следующий текст не надо принимать за дату или курс.
    if not (query.data or "").startswith("docfield:"):
        context.user_data.pop("awaiting_doc_field", None)

    # То же для файла, ждущего выбора типа: он нужен ветке scantype:
    if not (query.data or "").startswith("scantype:"):
        context.user_data.pop("pending_scan", None)

    data = query.data or ""

    # Меню реквизитов живёт в отдельном модуле: там своя многошаговая форма
    # и своё состояние в user_data. Клик по любой ДРУГОЙ кнопке эту форму
    # сбрасывает — иначе следующий текст уйдёт в неё вместо агента.
    if data.startswith("bp:"):
        await bank_ui.handle_callback(update, context, data)
        return
    if data.startswith("co:"):
        await company_ui.handle_callback(update, context, data,
                                         on_send=_make_card_sender(update, context))
        return
    if data.startswith("sign:"):
        await sign_ui.handle_callback(update, context, data, drive=agent.drive)
        return
    if data.startswith("sl:") or data.startswith("nd:"):
        bank_ui.clear_state(context)
        company_ui.clear_state(context)
        await salon_ui.handle_callback(update, context, data)
        return
    bank_ui.clear_state(context)
    company_ui.clear_state(context)
    salon_ui.clear_state(context)

    # ── Остальные кнопки — по таблице CALLBACK_ROUTES (модули cb_*.py) ──────
    for kind, value, handler in CALLBACK_ROUTES:
        if (data == value) if kind == "eq" else data.startswith(value):
            await handler(update, context, query, data)
            return
    logger.warning(f"Неизвестная кнопка: {data!r}")


# ─── ОБРАБОТКА ТЕКСТА ────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    user_text = update.message.text
    chat_id = str(update.effective_chat.id)

    # ── Форма реквизитов ────────────────────────────────────────────────────
    # Стоит первой: если бот спросил БИК, ответ «044525974» не должен уходить
    # в LLM и толковаться как что угодно ещё.
    if await bank_ui.handle_text(update, context, user_text):
        return
    if await company_ui.handle_text(update, context, user_text):
        return
    if await salon_ui.handle_text(update, context, agent, user_text):
        return

    # ── Ожидание недостающего поля для документа ────────────────────────────
    # Бот сам помнит, какой документ просили: записывает присланное значение
    # в журнал и повторяет сборку. Раньше возврат к документу зависел от того,
    # вспомнит ли о нём LLM, — и она его теряла.
    if context.user_data.get("awaiting_doc_field"):
        if await apply_doc_field(update, context, update.message, user_text):
            return

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

    if context.user_data.get("awaiting_deals_dates"):
        context.user_data["awaiting_deals_dates"] = False
        raw = user_text.strip()

        import re as _re
        matches = _re.findall(r"\d{1,2}\.\d{1,2}\.\d{4}", raw)
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ К срезам", callback_data="menu:deals"),
        ]])

        if not matches:
            await update.message.reply_text(
                "⚠️ Не нашёл ни одной даты. Ожидаю формат ДД.ММ.ГГГГ, например "
                "`01.06.2026 - 30.06.2026`",
                parse_mode="Markdown", reply_markup=back_kb,
            )
            return

        date_from = matches[0]
        date_to   = matches[1] if len(matches) >= 2 else ""  # пусто → до сегодня

        from datetime import datetime as _dt
        try:
            _df = _dt.strptime(date_from, "%d.%m.%Y").date()
            _dtend = _dt.strptime(date_to, "%d.%m.%Y").date() if date_to else None
            if _dtend and _df > _dtend:
                await update.message.reply_text(
                    f"⚠️ Начало периода `{date_from}` позже конца `{date_to}`. "
                    "Проверь порядок дат.",
                    parse_mode="Markdown", reply_markup=back_kb,
                )
                return
        except ValueError as e:
            await update.message.reply_text(
                f"⚠️ Не удалось разобрать дату: {e}", reply_markup=back_kb,
            )
            return

        context.user_data["deals_custom"] = (date_from, date_to)
        loading = await update.message.reply_text(
            f"🔄 Загружаю сделки за {date_from} — {date_to or 'сегодня'}..."
        )
        try:
            deals = await agent.sheets.get_all_deals()
        except Exception as e:
            logger.error(f"Ошибка получения сделок за свой период: {e}", exc_info=True)
            await loading.edit_text(f"⚠️ Ошибка: {e}", reply_markup=back_kb)
            return

        text_out, kb, _page = _build_deals_view(deals, "custom", "all", 0,
                                                date_from, date_to)
        context.user_data["last_deals_view"] = "deals:custom:all:0"
        await loading.edit_text(text_out, parse_mode="Markdown", reply_markup=kb)
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
        deal_date = user_text.strip()
        context.user_data["pending_deal_date"] = deal_date
        # Дата введена руками — дальше тот же второй шаг, что и после кнопки
        await ask_dkp_date(update.message, deal_date)
        return

    if context.user_data.get("awaiting_dkp_date"):
        context.user_data["awaiting_dkp_date"] = False
        deal_date = context.user_data.pop("pending_deal_date", "")
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(
                f"Дата договора: {deal_date}. Дата ДКП: {user_text.strip()}",
                chat_id=chat_id,
            )
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

    # ── Прислали просто номер сделки ────────────────────────────────────────
    # Отвечаем карточкой из журнала, не спрашивая LLM: она пересказывала данные
    # своими словами и цифры до пользователя не доходили. Заодно быстрее и без
    # лишнего вызова модели. Не нашли — пусть дальше разбирается LLM.
    if re.fullmatch(r"\d{9}", user_text.strip()):
        num = user_text.strip()
        deal = await agent.sheets.get_deal(num)
        if deal:
            context.user_data["current_deal"] = num
            await update.message.reply_text(
                _deal_card(deal),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Открыть меню сделки",
                                          callback_data=f"dealaction:{num}:menu")],
                    [InlineKeyboardButton("📄 Создать документы",
                                          callback_data=f"dealaction:{num}:docs")],
                    [InlineKeyboardButton("◀️ Меню", callback_data="menu:back")],
                ]),
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


def _deal_card(d: dict) -> str:
    """Краткая карточка сделки: кто, что, на сколько и в каком состоянии."""
    def v(key, default="—"):
        val = str(d.get(key, "") or "").strip()
        return val if val and val != "None" else default

    num    = v("Номер договора")
    vin    = v("car_vin", "")
    status = v("Статус")
    lines = [f"📄 *Сделка {num}* от {v('Дата договора')} · {status}", ""]

    buyer = v("buyer_name")
    lines.append(f"👤 Заказчик: {buyer}")
    lines.append(f"👤 Продавец: {v('seller_name')}")

    car = v("car_model")
    lines.append(f"🚗 {car}" + (f" · VIN `{vin}`" if vin else ""))

    # Суммы приводим к единому виду: в журнале они лежат по-разному —
    # «3060780», «3 137 299,50», иногда уже с символом ₽.
    price = _money_str(d.get("car_price"))
    if price:
        lines.append(f"💰 Цена авто: {price}")
    total = _money_str(d.get("Сумма Договора"))
    if total:
        lines.append(f"💵 Итого к оплате: *{total}*")

    # Оплаты показываем только когда по сделке уже что-то происходило —
    # у новой сделки три нулевые строки только зашумляют карточку.
    received  = _money_str(d.get("Получено"))
    remainder = _money_str(d.get("Остаток"))
    if received:
        lines.append(f"📥 Получено: {received}")
    if remainder:
        lines.append(f"⏳ Остаток: {remainder}")

    scans = v("Сканы", "")
    if scans:
        lines.append(f"🗂 Сканы: {scans}")

    return "\n".join(lines)


# ─── ОБРАБОТЧИК ОШИБОК ───────────────────────────────────────────────────────

async def error_handler(update, context):
    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Произошла ошибка, попробуйте ещё раз.")
        except Exception:
            pass


# ─── ЗАПУСК ──────────────────────────────────────────────────────────────────

# Список команд для меню Telegram («/» в чате) — раньше не регистрировался,
# поэтому единственным способом узнать список была просьба к разработчику
# или чтение bot.py. set_my_commands переносит его в сам Telegram.
async def _post_init(app):
    try:
        await app.bot.set_my_commands([
            BotCommand("start",           "Начать / перезапустить бота"),
            BotCommand("menu",            "Главное меню"),
            BotCommand("memory",          "Память бота — компании и инструкции"),
            BotCommand("clear",           "Очистить историю диалога"),
            BotCommand("del_instruction", "Удалить инструкцию по номеру"),
            BotCommand("backup",          "Бэкапы журнала сделок"),
            BotCommand("regen_docs",      "Пересобрать документы по старым сделкам"),
        ])
    except Exception as e:
        logger.error(f"Не удалось зарегистрировать список команд: {e}", exc_info=True)


def main():
    memory.init_db()
    # Банковские профили в памяти бота переводим на новую модель сразу при
    # старте: журнал мигрирует отдельно, профилей эта миграция не касается.
    try:
        moved = br.migrate_saved_profiles(memory)
        if moved:
            logger.info(f"Банковских профилей переведено на новую модель: {moved}")
    except Exception as e:
        logger.error(f"Не удалось перевести банковские профили: {e}", exc_info=True)
    memory.cleanup_old_pending_scans()
    settings_service.log_current_settings()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    # Таймауты выставлены под отправку крупных документов (docx/pdf ~1 МБ).
    # Дефолтный read_timeout=5s приводит к httpx.ReadTimeout при медленном
    # ответе Telegram — файл уходит, но клиент показывает «Произошла ошибка».
    app = (
        ApplicationBuilder()
        .token(token)
        .read_timeout(30)
        .write_timeout(60)
        .connect_timeout(20)
        .pool_timeout(20)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",           start))
    app.add_handler(CommandHandler("menu",            show_main_menu))
    app.add_handler(CommandHandler("memory",          show_memory))
    app.add_handler(CommandHandler("clear",           clear_history))
    app.add_handler(CommandHandler("del_instruction", del_instruction))
    app.add_handler(CommandHandler("backup",          cmd_backup))
    app.add_handler(CommandHandler("regen_docs",      cmd_regen_docs))
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

    # ── Разовая миграция блока реквизитов в журнале ──────────────────────
    # Идемпотентна: на уже мигрированной таблице стоит один запрос на чтение.
    if app.job_queue is not None:
        async def _migrate_requisites_job(_ctx):
            try:
                stats = await agent.sheets.migrate_requisites()
                logger.info(f"Миграция реквизитов: {stats}")
            except Exception as e:
                logger.error(f"Миграция колонок журнала не удалась: {e}", exc_info=True)
        app.job_queue.run_once(_migrate_requisites_job, when=5, name="migrate_requisites")

        # ── Сверка колонок журнала с кодом ───────────────────────────────
        # После миграции (when=5), чтобы читать уже итоговую раскладку.
        # При расхождении запись в журнал блокируется (gsheets_service),
        # а всем разрешённым чатам уходит сообщение со списком колонок.
        async def _verify_headers_job(ctx):
            res = await agent.sheets.verify_headers()
            if res.get("ok") is False:
                text = (
                    "🔒 Колонки журнала не совпадают с кодом бота — запись в журнал "
                    "и выпуск документов заблокированы.\n\n"
                    + agent.sheets.format_header_diffs(res["diffs"])
                    + "\n\nПоиск и просмотр сделок работают. Поправьте заголовки в строке 2 "
                      "таблицы (или обновите код) — блокировка снимется сама в течение минуты."
                )
                for chat_id in _get_allowed_chat_ids():
                    try:
                        await ctx.bot.send_message(chat_id=chat_id, text=text)
                    except Exception as e:
                        logger.warning(f"Не удалось уведомить {chat_id} о колонках журнала: {e}")
        app.job_queue.run_once(_verify_headers_job, when=20, name="verify_headers")

    # Разовый разбор деклараций (ИНН продавца, seller_inn) отработал
    # 10.09.2026 и здесь больше не запускается — код планировщика убран,
    # чтобы при следующих деплоях снова не звать Claude по журналу.
    # Реализация (agent.backfill_seller_inn_impl) осталась в agent.py на
    # случай, если понадобится прогнать ещё раз по пропущенным сделкам —
    # см. память проекта: seller-inn-backfill.

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
