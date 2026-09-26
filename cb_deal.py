"""
cb_deal.py — Работа с одной сделкой: карточка и действия, платежи, документы, даты, сканы, реквизиты.

Вынесено из bot.handle_callback 25.09.2026 без изменения логики — тела веток
перенесены дословно. Общие объекты берутся из bot_core, маршрутизация —
таблица CALLBACK_ROUTES в bot.py.
"""

import asyncio
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from agent import (
    _parse_payments,
    _calc_total_amount,
    _fmt_money,
    SCAN_LABELS,
    SCAN_PREFIXES,
    scan_status_text,
)
import memory
import bank_requisites as br
import salon
import sign_ui

from bot_core import (
    agent,
    apply_doc_field,
    ask_dkp_date,
    logger,
    run_doc_batch,
    run_doc_impl,
    send_result,
    typing_while,
)


async def on_deal_date(update, context, query, data):
    """Кнопки: callback_data «deal_date:…»."""
    value = data.split(":", 1)[1]
    await query.edit_message_reply_markup(reply_markup=None)

    if value == "__custom__":
        context.user_data["awaiting_deal_date"] = True
        await query.message.reply_text(
            "Введите дату договора в формате ДД.ММ.ГГГГ (например: 18.06.2026):"
        )
    else:
        context.user_data["pending_deal_date"] = value
        await ask_dkp_date(query.message, value)


async def on_dkp_date(update, context, query, data):
    """Кнопки: callback_data «dkp_date:…»."""
    value = data.split(":", 1)[1]
    await query.edit_message_reply_markup(reply_markup=None)

    if value == "__custom__":
        context.user_data["awaiting_dkp_date"] = True
        await query.message.reply_text(
            "Введите дату ДКП в формате ДД.ММ.ГГГГ (например: 18.06.2026):"
        )
    else:
        deal_date = context.user_data.pop("pending_deal_date", "")
        dkp_date  = deal_date if value == "__same__" else value
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(
                f"Дата договора: {deal_date}. Дата ДКП: {dkp_date}",
                chat_id=str(update.effective_chat.id),
            )
        )
        await send_result(query.message, result)


async def on_scantype(update, context, query, data):
    """Кнопки: callback_data «scantype:…»."""
    # Пользователь выбрал тип загружаемого скана — грузим на Drive под
    # осмысленным именем и пересчитываем статус комплекта.
    code = data.split(":", 1)[1]
    pending = context.user_data.pop("pending_scan", None)
    if not pending:
        await query.edit_message_text("⚠️ Файл потерялся, пришлите его ещё раз.")
        return

    num      = pending["num"]
    prefix   = SCAN_PREFIXES.get(code, "Прочее")
    if code == "ag":
        # Субагентская сделка: договор называется САГ_Договор — скан тоже.
        try:
            if salon.is_subagent(await agent.sheets.get_deal(pending["num"]) or {}):
                prefix = "Подп_САГ_Договор"
        except Exception as e:
            logger.warning(f"Тип сделки {pending['num']} не прочитан: {e}")
    filepath = pending["filepath"]
    ext      = Path(pending["filename"]).suffix or ".pdf"
    base     = f"{prefix}_{num}"

    await query.edit_message_text(f"⏳ Загружаю {SCAN_LABELS.get(code, 'скан')}...")
    try:
        scans_folder_id = await agent.drive._get_or_create_folder("Сканы", pending["folder_id"])

        # Имя занято (переснятый скан, вторая страница) — дописываем номер,
        # а не затираем: потерять уже подписанный документ хуже, чем
        # оставить в папке лишний файл.
        _, existing = await agent.list_scan_files(num)
        taken = {f.get("name", "") for f in existing}
        new_name, n = f"{base}{ext}", 1
        while new_name in taken:
            n += 1
            new_name = f"{base}_{n}{ext}"

        await agent.drive.upload_file(filepath, new_name, scans_folder_id)
        # Файл уже на Drive — повторно грузить его при create_contract не нужно
        memory.clear_pending_scans(str(update.effective_chat.id))
        status = await agent.refresh_scan_status(num)
    except Exception as e:
        logger.error(f"Ошибка загрузки скана для сделки {num}: {e}", exc_info=True)
        await query.message.reply_text(f"⚠️ Не удалось загрузить скан: {e}")
        return

    text = [f"✅ {SCAN_LABELS.get(code, 'Скан')} загружен: `{new_name}`"]
    if status:
        text.append(f"🗂 Сканы по сделке *{num}*: {status}")
    await query.message.reply_text(
        "\n".join(text),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📎 Загрузить ещё", callback_data=f"dealaction:{num}:scan")],
            [InlineKeyboardButton("◀️ К сделке",      callback_data=f"dealaction:{num}:menu")],
        ]),
    )


async def on_docfield(update, context, query, data):
    """Кнопки: callback_data «docfield:…»."""
    # Кнопка быстрого выбора даты расчёта. Значение уже готовое, поэтому
    # идёт в тот же apply_doc_field, что и ручной ввод.
    value = data.split(":", 1)[1]
    await query.edit_message_reply_markup(reply_markup=None)
    await apply_doc_field(update, context, query.message, value)


async def on_docmenu(update, context, query, data):
    """Кнопки: callback_data «docmenu:…»."""
    parts = data.split(":", 2)
    if len(parts) == 3:
        contract_number = parts[1]
        doc_type        = parts[2]
        await query.edit_message_reply_markup(reply_markup=None)

        # Закрывающие документы идут в порядке самой сделки: сначала
        # расписка (деньги выданы), затем акт и отчёт агента.
        # Строим их отдельными вызовами, а не одним пакетом: они требуют
        # полной оплаты, даты расчёта и фактического курса. Если сделка до
        # них ещё не дошла, базовый пакет всё равно уходит, а по каждому
        # недостающему документу приходит своя причина.
        CLOSING_STEPS = ("build_receipt", "build_act", "build_report")

        # full   = базовый пакет + закрывающие
        # closing = только закрывающие (сделка уже оплачена и рассчитана)
        in_batch = doc_type in ("full", "closing")

        if doc_type != "closing":
            base_type = "all" if doc_type == "full" else doc_type
            # Инструмент вызываем напрямую, без LLM: она пересказывала итог
            # своими словами и путала файлы с документами — писала «все 6
            # документов сформированы» там, где документов было три.
            await query.message.reply_text(
                f"⏳ Формирую документы по сделке {contract_number}..."
            )
            try:
                result = await typing_while(
                    update.effective_chat.id, context,
                    agent._execute_tool("generate_docs", {
                        "contract_number": contract_number,
                        "doc_type":        base_type,
                    }),
                )
            except Exception as e:
                logger.error(f"Ошибка generate_docs ({base_type}) для {contract_number}: {e}",
                             exc_info=True)
                result = {"message": f"⚠️ Ошибка создания документов: {e}"}

            files = []
            if result.get("file"):
                files.append({
                    "file":       result["file"],
                    "filename":   result["filename"],
                    "drive_link": result.get("drive_link", ""),
                })
            for f_path, f_name, f_link in zip(
                result.get("extra_files", []),
                result.get("extra_names", []),
                result.get("extra_links", [""] * len(result.get("extra_files", []))),
            ):
                files.append({"file": f_path, "filename": f_name, "drive_link": f_link})

            await send_result(query.message, {
                "files":   files,
                "text":    result.get("message", ""),
                # Внутри пакета кнопку «К сделке» гасим — она придёт одна в конце
                "buttons": None if in_batch else result.get("buttons"),
            }, context=context)

        if in_batch:
            await run_doc_batch(update, context, query.message,
                                contract_number, list(CLOSING_STEPS))


async def on_dealaction(update, context, query, data):
    """Кнопки: callback_data «dealaction:…»."""
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
        deal_bank      = br.normalize(deal)
        account_number = deal_bank["account_number"]
        bank_ben       = deal_bank["bank_name"]

        # Название профиля реквизитов.
        # Номера и типа мало: один счёт заведён несколькими профилями с разными
        # банками-корреспондентами, поэтому сверяются все поля реквизитов.
        _profiles = {n: memory.get_bank_profile(n) for n in memory.list_bank_profiles()}
        profile_name = br.match_profile(deal, _profiles) or "—"
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
        sub_lines = ""
        if salon.is_subagent(deal):
            sub_lines = (f"🏬 Салон (Агент): {deal.get('salon') or '—'}\n"
                         f"📑 Договор с клиентом: {deal.get('salon_contract') or '—'}\n")
        text = (
            f"📄 *Сделка {num}* от {date}"
            + (" · субагентская" if sub_lines else "") + "\n\n"
            + sub_lines
            + f"👤 {buyer}\n"
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
        # Статус сканов пересчитываем прямо здесь, а не берём из журнала:
        # файлы кладут в папку и напрямую с компьютера, мимо бота, и без
        # пересчёта в меню висели бы старые цифры. Один запрос к Drive.
        try:
            scans_status = await agent.refresh_scan_status(num)
        except Exception as e:
            logger.warning(f"Не удалось обновить статус сканов {num}: {e}")
            scans_status = str(deal.get("Сканы") or "").strip()
        if scans_status:
            text += f"\n🗂 Сканы: {scans_status}"
        keyboard = [
            [InlineKeyboardButton("📋 Создать документы", callback_data=f"dealaction:{num}:docs")],
            [InlineKeyboardButton("✏️ Изменить данные",   callback_data=f"dealaction:{num}:edit")],
            [InlineKeyboardButton("📎 Загрузить скан",    callback_data=f"dealaction:{num}:scan"),
             InlineKeyboardButton("🗂 Сканы",             callback_data=f"dealaction:{num}:scans")],
            [InlineKeyboardButton("💳 Оплаты",            callback_data=f"dealaction:{num}:payments")],
            # Расписка, акт и отчёт живут в меню «📋 Создать документы» —
            # в карточке сделки их не дублируем.
            [InlineKeyboardButton("✅ Завершить сделку",  callback_data=f"dealaction:{num}:complete")],
            [InlineKeyboardButton("❌ Отменить сделку",   callback_data=f"dealaction:{num}:cancel")],
        ]
        if folder:
            keyboard.insert(0, [InlineKeyboardButton("📁 Открыть на Drive", url=folder)])
        # Возврат туда, откуда пришли: тот же срез и та же страница
        # списка (запомнили в show_deals_list). Если пришли из поиска или
        # по номеру договора — в главное меню.
        back_cb = context.user_data.get("last_deals_view") or "menu:back"
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=back_cb)])
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

        # Заодно обновляем колонку «Сканы» в журнале: файлы могли положить
        # в папку и мимо бота, а колонка — только отражение папки.
        status = scan_status_text([f.get("name", "") for f in files])
        try:
            await agent.sheets.update_deal(num, {"Сканы": status})
        except Exception as e:
            logger.warning(f"Не удалось записать статус сканов {num}: {e}")

        if scans_id is None:
            text = f"🗂 *Сканы сделки {num}*\n\nПапка «Сканы» не найдена."
        elif not files:
            text = f"🗂 *Сканы сделки {num}*\n\nПапка пуста — сканов нет."
        else:
            lines = [f"🗂 *Сканы сделки {num}* · подписано {status}\n"]
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
        # Инструмент вызываем напрямую, без LLM: она переписывала готовый
        # текст своими словами и дописывала к нему технические коды
        # (all / ag / dkp / invoice), которых пользователь не набирает —
        # рядом стоят кнопки.
        await query.edit_message_text(f"⏳ Загружаю данные сделки {num}...")
        try:
            result = await typing_while(
                update.effective_chat.id, context,
                agent._execute_tool("check_deal", {"contract_number": num}),
            )
        except Exception as e:
            logger.error(f"Ошибка check_deal для {num}: {e}", exc_info=True)
            result = {"message": f"⚠️ Не удалось загрузить сделку {num}: {e}"}

        # Если сделка не готова (не хватает полей, не найдена, ошибка) —
        # у ответа нет своих кнопок. Без них из этого сообщения некуда
        # вернуться, и сделку приходилось вызывать заново через поиск.
        buttons = result.get("buttons") or [
            {"text": "🔄 Проверить снова", "callback_data": f"dealaction:{num}:docs"},
            {"text": "◀️ К сделке",        "callback_data": f"dealaction:{num}:menu"},
        ]
        await send_result(query.message, {
            "text":    result.get("message") or result.get("error", ""),
            "buttons": buttons,
        }, context=context)

    elif action in ("build_act", "build_receipt", "build_report"):
        # Акт выполненных услуг / расписка продавца о получении наличных
        # (Приложение № 1 к акту) / отчёт агента. Даты берутся автоматически
        # из колонки «Дата расчёта» либо из последнего платежа. Если сделка
        # не оплачена, нет фактического курса или данные неполные — *_impl
        # вернёт понятную ошибку. Сама логика — в run_doc_impl, она же
        # используется кнопкой «Всё».
        await run_doc_impl(update, context, query.message, num, action)

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


async def on_payrm(update, context, query, data):
    """Кнопки: callback_data «payrm:…»."""
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


async def on_payforce_confirm(update, context, query, data):
    """Кнопки: callback_data == «payforce:confirm»."""
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


async def on_editbank(update, context, query, data):
    """Кнопки: callback_data «editbank:…»."""
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
        # Пишем профиль через нормализацию: вместе с полями уедут и тип
        # счёта, и ИНН — иначе сделка получит банк одной юрисдикции и ИНН
        # другой, и в счёте это никак не проявится.
        ok = await agent.sheets.update_deal(
            contract_number, br.full_payload(profile, memory.get_setting)
        )
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


async def on_scan_route(update, context, query, data):
    """Кнопки: callback_data «scan_route:…»."""
    route    = data.split(":", 1)[1]
    filepath = context.user_data.pop("last_scan_filepath", None)
    filename = context.user_data.pop("last_scan_filename", "file")
    caption  = context.user_data.pop("last_scan_caption", "")

    if route == "new":
        # Новая сделка — читаем документ с нуля. Файл прислан без кнопки
        # «Новая сделка», значит сделка прямая: брошенный выбор салона не
        # должен сделать её субагентской.
        salon.clear_pending(str(update.effective_chat.id))
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

    elif route == "sign":
        # Подпись чужого документа: файл уже скачан, дальше всё в sign_ui
        await query.edit_message_text("✍️ Смотрю, где здесь подписывать…")
        await sign_ui.start(query.message, context, filepath, filename,
                            agent.builder, getter=memory.get_setting)

    elif route == "existing":
        # Сохраняем скан в папку существующей сделки
        context.user_data["awaiting_scan_for_existing"] = True
        context.user_data["pending_existing_filepath"]  = filepath
        context.user_data["pending_existing_filename"]  = filename
        await query.edit_message_text(
            "Укажи номер сделки (например: `280626001`):",
            parse_mode="Markdown",
        )


async def on_bankprofile(update, context, query, data):
    """Кнопки: callback_data «bankprofile:…»."""
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
