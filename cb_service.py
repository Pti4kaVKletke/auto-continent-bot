"""
cb_service.py — Служебные меню: бэкапы, настройки, пересборка документов, пересчёт сканов.

Вынесено из bot.handle_callback 25.09.2026 без изменения логики — тела веток
перенесены дословно. Общие объекты берутся из bot_core, маршрутизация —
таблица CALLBACK_ROUTES в bot.py.
"""

import time
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import settings_service

from bot_core import (
    _build_deals_view,
    _format_regen_report,
    _safe_reply,
    _select_deals,
    agent,
    backup,
    logger,
    rescan_menu_keyboard,
    setting_options_keyboard,
    settings_menu_keyboard,
    typing_while,
)


async def on_backup(update, context, query, data):
    """Кнопки: callback_data «backup:…»."""
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

        # Вместе с журналом — копия базы бота (компании, реквизиты, инструкции)
        db_result = await asyncio.to_thread(backup.create_db_backup)
        if db_result.get("success"):
            text += f"\n\n🗄 База бота: `{db_result['file_name']}` · {db_result['size_kb']} KB"
        else:
            db_err = str(db_result.get("error", "неизвестно")).replace("`", "'")
            text += f"\n\n⚠️ База бота не сохранена: `{db_err}`"
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


async def on_settings(update, context, query, data):
    """Кнопки: callback_data «settings:…»."""
    parts = data.split(":")
    try:
        setting_index = int(parts[1])
    except (IndexError, ValueError):
        await query.edit_message_text(
            "⚠️ Некорректный параметр настройки.",
            reply_markup=settings_menu_keyboard(),
        )
        return

    setting = settings_service.get_setting_by_index(setting_index)
    if not setting:
        await query.edit_message_text(
            "⚠️ Настройка не найдена.",
            reply_markup=settings_menu_keyboard(),
        )
        return

    # Клик по конкретному варианту — применяем через Railway API
    if len(parts) >= 3:
        try:
            option_index = int(parts[2])
        except ValueError:
            await query.edit_message_text(
                "⚠️ Некорректный вариант.",
                reply_markup=setting_options_keyboard(setting_index),
            )
            return

        option = settings_service.get_option_by_index(setting, option_index)
        if not option:
            await query.edit_message_text(
                "⚠️ Вариант не найден.",
                reply_markup=setting_options_keyboard(setting_index),
            )
            return

        # Если значение и так уже установлено — просто перерисуем меню
        if settings_service.get_current_value(setting) == option["value"]:
            await query.edit_message_text(
                f"ℹ️ *{setting['label']}* уже установлена в:\n"
                f"  {option['label']}",
                parse_mode="Markdown",
                reply_markup=setting_options_keyboard(setting_index),
            )
            return

        await query.edit_message_text(
            f"⏳ Обновляю *{setting['label']}*…",
            parse_mode="Markdown",
        )
        ok, err, needs_restart = await asyncio.to_thread(
            settings_service.apply_setting,
            setting, option["value"],
        )
        if ok and needs_restart:
            text = (
                f"✅ *{setting['label']}* обновлена:\n"
                f"  {option['label']}\n\n"
                "_Railway автоматически передеплоит сервис (~30 сек)._\n"
                "_После рестарта бот подхватит новое значение._"
            )
        elif ok:
            # Настройки из БД применяются к следующему же документу.
            text = (
                f"✅ *{setting['label']}* обновлена:\n"
                f"  {option['label']}\n\n"
                "_Применится со следующего документа, перезапуск не нужен._"
            )
        elif setting.get("storage") == "db":
            text = (
                f"❌ Не удалось сохранить *{setting['label']}*.\n\n"
                f"`{err}`"
            )
        else:
            text = (
                f"❌ Не удалось обновить *{setting['label']}*.\n\n"
                f"`{err}`\n\n"
                "Проверь `RAILWAY_API_TOKEN`, `RAILWAY_SERVICE_ID` и "
                "`RAILWAY_ENVIRONMENT_ID` в переменных сервиса."
            )
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=setting_options_keyboard(setting_index),
        )
        return

    # Клик по самой настройке — показать варианты
    current_label = settings_service.get_current_label(setting)
    where = ("настройка бота: `{}` (без перезапуска)".format(setting["key"])
             if setting.get("storage") == "db"
             else "Env-переменная: `{}`".format(setting["key"]))
    text = (
        f"*{setting['label']}*\n\n"
        f"Текущее значение: _{current_label}_\n\n"
        f"{where}"
    )
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=setting_options_keyboard(setting_index),
    )
    return


async def on_regendocs(update, context, query, data):
    """Кнопки: callback_data «regendocs:…»."""
    action = data.split(":", 1)[1]
    await query.edit_message_reply_markup(reply_markup=None)

    if action == "cancel":
        await query.message.reply_text("Отменено.")
        return

    if action != "run":
        return

    candidates = context.user_data.get("regen_docs_candidates") or []
    if not candidates:
        await query.message.reply_text(
            "Список кандидатов устарел (или бот перезапускался) — "
            "запустите /regen_docs заново."
        )
        return
    if context.bot_data.get("regen_docs_running"):
        await query.message.reply_text("⏳ Пересборка уже идёт.")
        return

    context.bot_data["regen_docs_running"] = True
    total = len(candidates)
    await query.message.reply_text(
        f"🚀 Начинаю пересборку по {total} сделкам. Буду отчитываться каждые 10."
    )

    progress = {"last": 0}

    async def _progress(num, i, tot):
        if i == 1 or i == tot or i - progress["last"] >= 10:
            progress["last"] = i
            await query.message.reply_text(f"⏳ {i}/{tot} — сделка {num}...")

    try:
        summary = await agent.regenerate_missing_docs_impl(candidates, progress_cb=_progress)
    except Exception as e:
        logger.error(f"Пересборка документов упала: {e}", exc_info=True)
        await query.message.reply_text(f"⚠️ Прогон прерван ошибкой: {e}")
        context.bot_data["regen_docs_running"] = False
        return

    context.bot_data["regen_docs_running"] = False
    context.user_data.pop("regen_docs_candidates", None)
    await _safe_reply(query.message, _format_regen_report(summary), parse_mode="Markdown")
    return


async def on_rescan(update, context, query, data):
    """Кнопки: callback_data «rescan:…»."""
    parts = data.split(":")

    if len(parts) == 2 and parts[1] == "menu":
        await query.edit_message_text(
            "🔄 *Обновление сканов*\n\n"
            "Перечитаю папки «Сканы» на Drive и пропишу статусы в журнал.\n"
            "За какой срез обновляем?",
            parse_mode="Markdown",
            reply_markup=rescan_menu_keyboard(),
        )
        return

    # rescan:force:<period>:<status> — повтор в обход троттлинга
    force = len(parts) > 3 and parts[1] == "force"
    if force:
        period, status_code = parts[2], parts[3]
    else:
        period      = parts[1] if len(parts) > 1 else "all"
        status_code = parts[2] if len(parts) > 2 else "all"

    df, dt = context.user_data.get("deals_custom", ("", ""))

    # Троттлинг: подряд идущие нажатия не гоняют Drive впустую.
    ago = int(time.time() - context.user_data.get("last_rescan_ts", 0))
    if not force and ago < 120:
        await query.edit_message_text(
            f"⏳ Сканы обновлялись {ago} сек назад.\n"
            "Если с тех пор ничего не докладывал в папки — обновлять нечего.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🔄 Всё равно обновить",
                    callback_data=f"rescan:force:{period}:{status_code}")],
                [InlineKeyboardButton(
                    "📋 Показать список",
                    callback_data=f"deals:{period}:{status_code}:0")],
                [InlineKeyboardButton("◀️ К срезам", callback_data="menu:deals")],
            ]),
        )
        return

    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ К срезам", callback_data="menu:deals"),
    ]])

    await query.edit_message_text("🔄 Читаю папки «Сканы» на Drive...")
    try:
        deals = await agent.sheets.get_all_deals()
    except Exception as e:
        logger.error(f"Ошибка получения сделок перед обновлением сканов: {e}",
                     exc_info=True)
        await query.edit_message_text(f"⚠️ Ошибка: {e}", reply_markup=back_kb)
        return

    rows, _present, label, status_code = _select_deals(
        deals, period, status_code, df, dt)
    if not rows:
        await query.edit_message_text(
            f"📋 За период «{label}» сделок нет — обновлять нечего.",
            reply_markup=back_kb,
        )
        return

    try:
        res = await typing_while(
            update.effective_chat.id, context,
            agent.refresh_scans_bulk(rows),
        )
    except Exception as e:
        logger.error(f"Ошибка массового обновления сканов: {e}", exc_info=True)
        await query.edit_message_text(f"⚠️ Ошибка обновления сканов: {e}",
                                      reply_markup=back_kb)
        return

    context.user_data["last_rescan_ts"] = time.time()

    # Свежие статусы подставляем в уже загруженный журнал — второй раз
    # читать лист незачем.
    statuses = res.get("statuses") or {}
    for d in deals:
        num = (d.get("Номер договора") or "").strip()
        if num in statuses:
            d["Сканы"] = statuses[num]

    note = (f"_🔄 Проверено {res['checked']} · обновлено {res['updated']} · "
            f"без изменений {max(0, res['checked'] - res['updated'])}")
    if res.get("skipped"):
        note += f" · без папки на Drive {res['skipped']}"
    note += "_"

    text, kb, page = _build_deals_view(deals, period, status_code, 0, df, dt,
                                       note=note)
    context.user_data["last_deals_view"] = f"deals:{period}:{status_code}:{page}"
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.warning(f"Не удалось показать список после обновления сканов: {e}")
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return
