"""
cb_deal.py — Работа с одной сделкой: карточка и действия, платежи, документы, даты, сканы, реквизиты.

Вынесено из bot.handle_callback 25.09.2026 без изменения логики — тела веток
перенесены дословно. Общие объекты берутся из bot_core, маршрутизация —
таблица CALLBACK_ROUTES в bot.py.
"""

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from agent import (
    _parse_payments,
    _calc_total_amount,
    _fmt_money,
    SCAN_LABELS,
    SCAN_PREFIXES,
    SCAN_TAGS,
    is_merged_scan,
    SCAN_TYPES,
    scan_multi_name,
    scan_codes_sorted,
    scan_status_text,
)
import memory
import bank_requisites as br
import kg_deal
import salon
import sign_ui
from scan_split import (format_range, parse_ranges, prepare_source,
                        ranges_from_page_labels, split_pdf)
from scan_merge import _is_image, merge_parts, merged_name, plan_merge
from scan_upload import auto_scan_type, deal_summary, images_to_pdf, is_image_name

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
        msg = f"Дата договора: {deal_date}. Дата ДКП: {dkp_date}"
        if value == "__none__":
            msg = f"Дата договора: {deal_date}. Сделка «Учёт КГ» — даты ДКП нет, она будет на этапе 2."
        result = await typing_while(
            update.effective_chat.id, context,
            agent.process_message(
                msg,
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
    await upload_scan(query.message, str(update.effective_chat.id), pending, code,
                      edit_query=query)


def scan_type_keyboard(contract_number: str, is_kg: bool):
    """Кнопки выбора типа скана (спец/СРТС/доверенность — только «Учёт КГ»)."""
    kb = [[InlineKeyboardButton(label, callback_data=f"scantype:{code}")]
          for code, label, _, _ in SCAN_TYPES
          if is_kg or code not in ("spec", "srts", "poa")]
    kb.append([InlineKeyboardButton("📚 Несколько документов в одном файле",
                                    callback_data="scanmulti:open")])
    kb.append([InlineKeyboardButton("◀️ Отмена",
                                    callback_data=f"dealaction:{contract_number}:menu")])
    return InlineKeyboardMarkup(kb)


# Что можно отметить внутри общего файла (комплект «Скан_АД», СРТС и прочее
# сюда не входят: у них своя кнопка).
_MULTI_CODES = ("ag", "dkp", "receipt", "report", "act", "spec", "invoice", "id")


def scan_multi_keyboard(selected, is_kg: bool):
    """Список документов с галочками для общего файла."""
    sel = set(selected or ())
    kb = []
    for code, label, _, _ in SCAN_TYPES:
        if code not in _MULTI_CODES or (code == "spec" and not is_kg):
            continue
        mark = "☑" if code in sel else "☐"
        kb.append([InlineKeyboardButton(f"{mark} {label.replace(' (Учёт КГ)', '')}",
                                        callback_data=f"scanmulti:t:{code}")])
    kb.append([InlineKeyboardButton(f"✅ Готово ({len(sel)})" if sel else "✅ Готово",
                                    callback_data="scanmulti:done")])
    kb.append([InlineKeyboardButton("◀️ Назад к списку", callback_data="scanmulti:back")])
    return InlineKeyboardMarkup(kb)


async def on_scanmulti(update, context, query, data):
    """Кнопки: callback_data «scanmulti:…» — общий файл с несколькими документами."""
    action = data.split(":", 1)[1]
    pending = context.user_data.get("pending_scan")
    if not pending:
        await query.edit_message_text("⚠️ Файл потерялся, пришлите его ещё раз.")
        return
    num, is_kg = pending["num"], pending.get("is_kg", False)
    sel = pending.setdefault("multi", [])

    async def show(warn=""):
        text = (f"📚 Какие документы в файле по сделке *{num}*?\n"
                "Отметьте все и нажмите «Готово».")
        if warn:
            text = f"⚠️ {warn}\n" + text
        try:
            await query.edit_message_text(text, parse_mode="Markdown",
                                          reply_markup=scan_multi_keyboard(sel, is_kg))
        except Exception:
            pass  # то же самое содержимое — Telegram не даёт отредактировать

    if action == "open":
        await show()
    elif action.startswith("t:"):
        code = action[2:]
        if code in sel:
            sel.remove(code)
        elif code in _MULTI_CODES:
            sel.append(code)
        await show()
    elif action == "back":
        pending.pop("multi", None)
        await query.edit_message_text(
            f"📎 Файл получен. Что это за документ по сделке *{num}*?",
            parse_mode="Markdown", reply_markup=scan_type_keyboard(num, is_kg))
    elif action == "done":
        if not sel:
            await show("Отметьте хотя бы один документ.")
            return
        context.user_data.pop("pending_scan", None)
        codes = scan_codes_sorted(sel)
        chat_id = str(update.effective_chat.id)
        if len(codes) == 1:
            await upload_scan(query.message, chat_id, pending, codes[0], edit_query=query)
        else:
            # Несколько документов: делим на отдельные сканы (с подтверждением).
            await start_split(query.message, context, chat_id, pending, codes,
                              edit_query=query)


def existing_prompt_keyboard(context):
    """Под вопросом «номер сделки?»: кнопка последней открытой сделки."""
    kb = []
    cur = context.user_data.get("current_deal")
    if cur:
        kb.append([InlineKeyboardButton(f"📂 Сделка {cur} (последняя открытая)",
                                        callback_data=f"scanexist:{cur}")])
    kb.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(kb)


async def begin_scan_upload(message, context, chat_id, num, folder_id, filepath,
                            filename, pages=None, show_summary=False) -> bool:
    """Общий вход для обоих путей загрузки скана в сделку.

    Путь 1 (файл прислан без контекста → «Сохранить скан в существующую
    сделку» → номер) и путь 2 (карточка сделки → «📎 Загрузить скан») дальше
    идут одинаково: тип по имени файла или кнопкой, фото → PDF, статус «Сканы».

    show_summary — проверить, что сделка есть, и показать сводку (путь 1, где
    номер вводят руками). Возвращает False, если сделка не найдена: вызывающий
    оставляет ожидание номера.
    """
    try:
        deal = await agent.sheets.get_deal(num)
    except Exception as e:
        logger.warning(f"Сделка {num} не прочитана: {e}")
        deal = None

    if show_summary:
        if not deal:
            await message.reply_text(
                f"❌ Сделка {num} не найдена. Проверьте номер и введите ещё раз.",
                reply_markup=existing_prompt_keyboard(context))
            return False
        await message.reply_text(deal_summary(deal, num))

    if not folder_id:
        link = (deal or {}).get("Папка Drive", "") or ""
        folder_id = (link.split("/folders/")[-1].split("?")[0]
                     if "/folders/" in link else "")
        if not folder_id:
            try:
                folder_id = await agent.drive.get_or_create_deal_folder(num)
            except Exception as e:
                logger.error(f"Папка сделки {num}: {e}", exc_info=True)
                await message.reply_text(f"⚠️ Не удалось открыть папку сделки {num}: {e}")
                return True

    try:
        is_kg = kg_deal.is_kg(deal or {})
    except Exception:
        is_kg = False
    is_img = is_image_name(filename)
    pending = {
        "num":       num,
        "folder_id": folder_id,
        "filepath":  filepath,
        "filename":  filename,
        "is_kg":     is_kg,
        "pages":     (pages or [filepath]) if is_img else None,
        "ts":        time.time(),
    }

    # Файл уже назван по правилам («Подп_ДКП_ТС_…», «Скан_АД_…») — тип берём из
    # имени и не спрашиваем. Номер другой сделки в имени отключает автотип.
    allowed = [code for code, _, _, _ in SCAN_TYPES
               if is_kg or code not in ("spec", "srts", "poa")]
    auto_code, warn = auto_scan_type(filename, num, allowed)
    if isinstance(auto_code, tuple):
        # Файл назван как общий («Подп_АГ+ДКП_…») — делим на отдельные сканы.
        await start_split(message, context, chat_id, pending, auto_code)
        return True
    if auto_code:
        await upload_scan(message, chat_id, pending, auto_code, auto=True)
        return True

    text = f"📎 Файл получен. Что это за документ по сделке *{num}*?"
    if pending["pages"] and len(pending["pages"]) > 1:
        text = (f"📎 Страниц: {len(pending['pages'])}. Пришлите остальные или "
                f"выберите тип документа по сделке *{num}*:")
    if warn:
        text = f"⚠️ {warn}.\n" + text
    if is_img:
        text += "\n_Если страниц несколько — пришлите остальные фото, потом выберите тип._"
    m = await message.reply_text(text, parse_mode="Markdown",
                                 reply_markup=scan_type_keyboard(num, is_kg))
    pending["prompt_id"] = m.message_id
    context.user_data["pending_scan"] = pending
    return True


async def build_merged_scan(num: str) -> dict:
    """Собирает «Комплект_<номер>….pdf» из сканов сделки и кладёт в папку «Сканы».

    Порядок безопасный: новый файл сначала загружается, и только потом старый
    единый файл (если был) уходит в корзину — потерять нечего. Если хоть один
    файл не прочитался, единый PDF не собирается: имя «5 из 5» при дыре в
    содержимом было бы ложью.
    """
    scans_id, files = await agent.list_scan_files(num)
    if not scans_id:
        return {"ok": False, "error": "Папка «Сканы» не найдена — сканов ещё не загружали."}
    deal = await agent.sheets.get_deal(num) or {}
    try:
        kg = kg_deal.is_kg(deal)
    except Exception:
        kg = False
    plan = plan_merge(files or [], kg=kg)
    if not plan["items"]:
        return {"ok": False,
                "error": "В папке «Сканы» нет подписанных документов (Подп_…) для сборки."}

    parts, bad = [], []
    for f in plan["items"]:
        data = await agent.drive.download_file_bytes(f["id"])
        if not data:
            bad.append(f["name"])
        else:
            parts.append((f["name"], data, _is_image(f)))
    if bad:
        return {"ok": False, "error": "Не удалось скачать: " + ", ".join(bad)
                                      + ". Единый PDF не собран."}

    tmp = tempfile.mkdtemp(prefix="merge_")
    try:
        out = os.path.join(tmp, "merged.pdf")
        pages, unreadable = await asyncio.to_thread(merge_parts, parts, out)
        if unreadable or not pages:
            return {"ok": False, "error": "Не удалось прочитать как PDF/фото: "
                                          + ", ".join(unreadable or ["все файлы"])
                                          + ". Единый PDF не собран."}
        name = merged_name(num, plan)
        link = await agent.drive.upload_file(out, name, scans_id)
        if not link:
            return {"ok": False, "error": "Не удалось загрузить единый PDF на Drive."}
        # Прежние единые файлы (имя могло быть другим — комплект был неполный)
        for f in files or []:
            if is_merged_scan(f.get("name", "")):
                await agent.drive.trash_file(f["id"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    status = await agent.refresh_scan_status(num)
    missing_labels = [SCAN_LABELS[c].replace(" (Учёт КГ)", "") for c in plan["missing"]]
    return {"ok": True, "name": name, "link": link, "pages": pages,
            "included": [f["name"] for f in plan["items"]],
            "missing_labels": missing_labels,
            "dups": [SCAN_LABELS[c] for c in plan["dups"]],
            "skipped": plan["skipped"], "status": status}


def _split_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Верно, разделить", callback_data="scansplit:ok")],
        [InlineKeyboardButton("✏️ Исправить страницы", callback_data="scansplit:edit")],
        [InlineKeyboardButton("📎 Оставить одним файлом", callback_data="scansplit:keep")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="scansplit:cancel")],
    ])


async def show_split_confirm(message, pending, auto=False):
    """Показывает разбивку по страницам и просит подтвердить."""
    sp = pending["split"]
    lines = [f"📑 В файле {sp['n']} стр. " +
             ("Разбивка определена автоматически — проверьте:" if auto else "Разбивка:")]
    for code in scan_codes_sorted(sp["codes"]):
        label = SCAN_LABELS[code].replace(" (Учёт КГ)", "")
        lines.append(f"• {label} — {format_range(sp['ranges'][code])}")
    covered = {p for a, b in sp["ranges"].values() for p in range(a, b + 1)}
    rest = [p for p in range(1, sp["n"] + 1) if p not in covered]
    if rest:
        lines.append("⚠️ Не войдут (не отмечены): стр. " + ", ".join(map(str, rest)))
    await message.reply_text("\n".join(lines), reply_markup=_split_keyboard())


async def ask_split_ranges(message, context, pending, note=""):
    """Просит ввести страницы вручную: «АГ 1-4, ДКП 5-6»."""
    sp = pending["split"]
    codes = scan_codes_sorted(sp["codes"])
    example = ", ".join(f"{SCAN_TAGS[c]} {i}" for i, c in enumerate(codes, 1) if c in SCAN_TAGS)
    context.user_data["awaiting_scan_ranges"] = True
    text = (f"{note}Пришлите страницы каждого документа — например: {example}\n"
            f"(диапазон — через дефис: АГ 1-4). В файле {sp['n']} стр.\n"
            "Метки: " + ", ".join(SCAN_TAGS[c] for c in codes if c in SCAN_TAGS))
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📎 Оставить одним файлом", callback_data="scansplit:keep")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="scansplit:cancel")],
    ]))


async def start_split(message, context, chat_id, pending, codes, edit_query=None):
    """В файле несколько документов: определить страницы и предложить разбивку."""
    codes = list(codes)
    progress = "⏳ Определяю, где какой документ..."
    if edit_query is not None:
        await edit_query.edit_message_text(progress)
    else:
        await message.reply_text(progress)

    src, n = None, 0
    try:
        out = str(Path(pending["filepath"]).with_name(f"split_src_{int(time.time())}.pdf"))
        src, n = await asyncio.to_thread(prepare_source, pending, out)
    except Exception as e:
        logger.warning(f"Разделение {pending['num']}: источник не подготовлен: {e}")
    if not src or n < len(codes):
        why = ("Не удалось разобрать файл." if not src
               else f"В файле {n} стр. — меньше, чем документов.")
        await message.reply_text(f"{why} Сохраняю одним файлом.")
        await upload_scan(message, chat_id, pending, codes)
        return
    if src != pending["filepath"]:
        memory.add_pending_scan(str(chat_id), src, "split_source.pdf")  # уберётся с остальными

    labels = None
    try:
        labels = await agent.classify_scan_pages(src, codes)
    except Exception as e:
        logger.warning(f"Разделение {pending['num']}: страницы не определены: {e}")
    ranges = ranges_from_page_labels(labels, codes)

    pending["split"] = {"codes": codes, "src": src, "n": n, "ranges": ranges}
    context.user_data["pending_scan"] = pending
    if ranges:
        await show_split_confirm(message, pending, auto=True)
    else:
        await ask_split_ranges(message, context, pending,
                               "Не удалось определить страницы автоматически. ")


async def _split_and_upload(message, chat_id, pending):
    """Режет файл по подтверждённой разбивке и кладёт каждый документ отдельно."""
    sp, num = pending["split"], pending["num"]
    out_dir = str(Path(sp["src"]).parent)
    parts = await asyncio.to_thread(split_pdf, sp["src"], sp["ranges"], out_dir,
                                    str(int(time.time())))
    done = []
    try:
        for code in scan_codes_sorted(sp["codes"]):
            base = await _scan_base(pending, code, ".pdf")
            name = await _put_scan(pending, parts[code], base, ".pdf")
            done.append((name, sp["ranges"][code]))
        memory.clear_pending_scans(str(chat_id))
        status = await agent.refresh_scan_status(num)
    except Exception as e:
        logger.error(f"Разделение скана {num}: {e}", exc_info=True)
        text = f"⚠️ Не удалось загрузить: {e}"
        if done:
            text += "\nУже загружены: " + ", ".join(n for n, _ in done)
        await message.reply_text(text)
        return
    finally:
        for p in parts.values():
            try:
                os.remove(p)
            except OSError:
                pass

    lines = ["✅ Файл разделён, документы сохранены отдельно:"]
    lines += [f"• {n} ({format_range(r)})" for n, r in done]
    if status:
        lines.append(f"🗂 Сканы по сделке {num}: {status}")
    await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📎 Загрузить ещё", callback_data=f"dealaction:{num}:scan")],
        [InlineKeyboardButton("📚 Собрать единый PDF", callback_data=f"dealaction:{num}:merge")],
        [InlineKeyboardButton("◀️ К сделке", callback_data=f"dealaction:{num}:menu")],
    ]))


async def on_scansplit(update, context, query, data):
    """Кнопки: callback_data «scansplit:…» — подтверждение разбивки общего файла."""
    action = data.split(":", 1)[1]
    chat_id = str(update.effective_chat.id)
    pending = context.user_data.get("pending_scan")
    sp = (pending or {}).get("split")
    if not sp:
        await query.edit_message_text("⚠️ Файл потерялся, пришлите его ещё раз.")
        return

    if action == "ok":
        if not sp.get("ranges"):
            await ask_split_ranges(query.message, context, pending)
            return
        context.user_data.pop("pending_scan", None)
        await query.edit_message_text("⏳ Делю файл и загружаю...")
        await _split_and_upload(query.message, chat_id, pending)
    elif action == "edit":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await ask_split_ranges(query.message, context, pending)
    elif action == "keep":
        context.user_data.pop("pending_scan", None)
        context.user_data.pop("awaiting_scan_ranges", None)
        await upload_scan(query.message, chat_id, pending, list(sp["codes"]),
                          edit_query=query)
    elif action == "cancel":
        context.user_data.pop("pending_scan", None)
        context.user_data.pop("awaiting_scan_ranges", None)
        memory.clear_pending_scans(chat_id)
        await query.edit_message_text("Отменено, файл не сохранён.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К сделке", callback_data=f"dealaction:{pending['num']}:menu")]]))


async def on_scanexist(update, context, query, data):
    """Кнопка «📂 Сделка …» под вопросом о номере: callback_data «scanexist:<номер>»."""
    num = data.split(":", 1)[1]
    ex = context.user_data.pop("existing_scan", None)
    if not ex or not Path(ex["filepath"]).exists():
        await query.edit_message_text("⚠️ Файл потерялся, пришлите его ещё раз.")
        return
    await query.edit_message_text(f"⏳ Проверяю сделку {num}...")
    ok = await begin_scan_upload(query.message, context, str(update.effective_chat.id),
                                 num, None, ex["filepath"], ex["filename"],
                                 ex.get("pages"), show_summary=True)
    if not ok:
        context.user_data["awaiting_scan_for_existing"] = True
        context.user_data["existing_scan"] = ex


async def _scan_base(pending, code, ext) -> str:
    """Имя скана одного типа без расширения: «Подп_ДКП_ТС_<номер>»."""
    num    = pending["num"]
    prefix = SCAN_PREFIXES.get(code, "Прочее")
    if code == "ag":
        # Субагентская сделка: договор называется САГ_Договор — скан тоже.
        try:
            if salon.is_subagent(await agent.sheets.get_deal(num) or {}):
                prefix = "Подп_САГ_Договор"
        except Exception as e:
            logger.warning(f"Тип сделки {num} не прочитан: {e}")
    base = f"{prefix}_{num}"
    if code in ("srts", "poa"):
        # «Учёт КГ»: СРТС_XXXXXX / Доверенность_XXXXXX — по 6 знакам VIN,
        # как номер ДКП. Нет VIN в журнале — по номеру сделки.
        try:
            _vin = (await agent.sheets.get_deal(num) or {}).get("car_vin", "")
        except Exception:
            _vin = ""
        base = kg_deal.drive_name(code, num, _vin, ext)[:-len(ext)]
    return base


async def _put_scan(pending, filepath, base, ext) -> str:
    """Кладёт файл в папку «Сканы» сделки, возвращает имя на Drive."""
    num = pending["num"]
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
    return new_name


async def upload_scan(message, chat_id, pending, code, edit_query=None, auto=False):
    """Кладёт скан в папку «Сканы» сделки под именем по типу и пересчитывает статус.

    pending — {num, folder_id, filepath, filename[, pages]}. Если есть pages
    (фото страниц), они склеиваются в один PDF. auto=True — тип взят из имени
    файла, а не выбран кнопкой (только для строки в ответе).
    """
    num      = pending["num"]
    multi    = list(code) if isinstance(code, (list, tuple)) else None
    if multi:
        # Несколько документов в одном файле: «Подп_АГ+ДКП+Акт» — метки читает
        # scan_codes_of, поэтому по имени видно (и считается), что внутри.
        prefix = scan_multi_name(multi)
        label  = ", ".join(SCAN_LABELS[c].replace(" (Учёт КГ)", "") for c in multi)
    else:
        label  = SCAN_LABELS.get(code, "скан")
    filepath = pending["filepath"]
    ext      = Path(pending["filename"]).suffix or ".pdf"
    base     = (f"{prefix}_{num}" if multi
                else await _scan_base(pending, code, ext))

    progress = f"⏳ Загружаю: {label}..."
    if edit_query is not None:
        await edit_query.edit_message_text(progress)
    else:
        await message.reply_text(progress)

    made_pdf = None
    try:
        pages = pending.get("pages")
        if pages or is_image_name(pending["filename"]):
            # Фото документа → PDF (несколько страниц — один файл)
            made_pdf = str(Path(filepath).with_name(f"scan_{int(time.time())}.pdf"))
            await asyncio.to_thread(images_to_pdf, pages or [filepath], made_pdf)
            filepath, ext = made_pdf, ".pdf"
        new_name = await _put_scan(pending, filepath, base, ext)
        # Файл уже на Drive — повторно грузить его при create_contract не нужно
        memory.clear_pending_scans(str(chat_id))
        status = await agent.refresh_scan_status(num)
    except Exception as e:
        logger.error(f"Ошибка загрузки скана для сделки {num}: {e}", exc_info=True)
        await message.reply_text(f"⚠️ Не удалось загрузить скан: {e}")
        return
    finally:
        if made_pdf and os.path.exists(made_pdf):
            try:
                os.remove(made_pdf)
            except OSError:
                pass

    text = [f"✅ Загружено ({label}): `{new_name}`" if multi
            else f"✅ {label[:1].upper() + label[1:]} загружен: `{new_name}`"]
    if auto:
        text.append("🔎 Тип определён по имени файла")
    if pages and len(pages) > 1:
        text.append(f"📄 Страниц в PDF: {len(pages)}")
    if status:
        text.append(f"🗂 Сканы по сделке *{num}*: {status}")
    await message.reply_text(
        "\n".join(text),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📎 Загрузить ещё", callback_data=f"dealaction:{num}:scan")],
            [InlineKeyboardButton("📚 Собрать единый PDF", callback_data=f"dealaction:{num}:merge")],
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
        is_kg = kg_deal.is_kg(deal)
        kg_stage2 = is_kg and kg_deal.stage2_done(deal)
        if is_kg and not kg_stage2:
            car_line = f"🚗 {kg_deal.car_line(deal) or car}\n🇰🇬 Данные ТС ещё не внесены\n"
            seller_line = ""
        elif is_kg:
            car_line = (f"🚗 {car} · VIN `{vin}`\n"
                        f"📘 СРТС: {deal.get('srts') or '—'} · {deal.get('gos_number') or '—'}\n"
                        f"📜 Доверенность: {deal.get('poa') or 'не внесена'}\n")
            seller_line = f"👤 {seller}\n"
        else:
            car_line = f"🚗 {car} · VIN `{vin}`\n"
            seller_line = f"👤 {seller}\n"
        status_txt = str(deal.get("Статус") or "").strip()
        text = (
            f"📄 *Сделка {num}* от {date}"
            + (" · субагентская" if sub_lines else "")
            + (" · Учёт КГ" if is_kg else "")
            + (f" · {status_txt}" if is_kg and status_txt else "") + "\n\n"
            + sub_lines
            + f"👤 {buyer}\n"
            + seller_line
            + car_line
            + f"💰 Цена авто: {price} руб."
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
        # «Учёт КГ»: этап 2. Кнопка видна, пока данные ТС не внесены целиком
        # (в том числе пока нет доверенности — её вносят позже тем же путём).
        if is_kg and (not kg_stage2 or not str(deal.get("poa") or "").strip()
                      or status_txt.lower() == kg_deal.STATUS_PODBOR.lower()):
            keyboard.insert(1, [InlineKeyboardButton("🚗 Внести данные ТС",
                                                     callback_data=f"dealaction:{num}:kg_vehicle")])
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
        status = scan_status_text([f.get("name", "") for f in files],
                                  kg=kg_deal.is_kg(deal or {}))
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
        if files:
            kb.append([InlineKeyboardButton("📚 Собрать единый PDF",
                                            callback_data=f"dealaction:{num}:merge")])
        kb.append([InlineKeyboardButton("◀️ Назад",          callback_data=f"dealaction:{num}:menu")])

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
            disable_web_page_preview=True,
        )

    elif action == "merge":
        await query.edit_message_text(f"⏳ Собираю единый PDF по сделке {num}...")
        try:
            r = await build_merged_scan(num)
        except Exception as e:
            logger.error(f"Единый PDF {num}: {e}", exc_info=True)
            r = {"ok": False, "error": f"Не удалось собрать: {e}"}
        kb = []
        if r.get("ok") and r.get("link"):
            kb.append([InlineKeyboardButton("📄 Открыть единый PDF", url=r["link"])])
        kb.append([InlineKeyboardButton("🗂 Сканы",    callback_data=f"dealaction:{num}:scans")])
        kb.append([InlineKeyboardButton("◀️ К сделке", callback_data=f"dealaction:{num}:menu")])
        if not r.get("ok"):
            text = f"⚠️ {r.get('error')}"
        else:
            lines = [f"✅ Единый PDF собран: {r['name']}",
                     f"📄 Страниц: {r['pages']} · файлов в нём: {len(r['included'])}"]
            lines += [f"   {i}. {n}" for i, n in enumerate(r["included"], 1)]
            if r["missing_labels"]:
                lines.append(f"⚠️ Комплект неполный, не хватает: {', '.join(r['missing_labels'])}")
            if r["dups"]:
                lines.append(f"⚠️ Есть повторы ({', '.join(r['dups'])}) — вошли все версии, "
                             "лишнюю можно убрать из «Сканы» и собрать заново")
            if r["skipped"]:
                lines.append(f"Не вошли (не подписанные документы): {len(r['skipped'])}")
            if r.get("status"):
                lines.append(f"🗂 Сканы: {r['status']}")
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                      disable_web_page_preview=True)

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

    elif action == "kg_vehicle":
        # «Учёт КГ», этап 2. Сделка запоминается у бота (kg_deal), файлы
        # копятся там же с типом, выбранным кнопкой; LLM получает напоминание
        # в промпте и в конце вызывает set_kg_vehicle.
        chat_id = str(update.effective_chat.id)
        deal = await agent.sheets.get_deal(num) or {}
        if not kg_deal.is_kg(deal):
            await query.edit_message_text(f"Сделка {num} — не «Учёт КГ».")
            return
        memory.clear_history(chat_id)
        memory.clear_pending_scans(chat_id)
        kg_deal.clear_new_pending(chat_id)
        kg_deal.set_vehicle_pending(chat_id, num)
        context.user_data["current_deal"] = num
        have = ""
        if kg_deal.stage2_done(deal):
            have = ("\n\nУже внесено: VIN " + str(deal.get("car_vin") or "—")
                    + ", ДКП от " + str(deal.get("Дата ДКП") or "—")
                    + ". Пришли то, что нужно добавить или исправить (например доверенность).")
        await query.edit_message_text(
            f"🚗 *Внести данные ТС — сделка {num}*\n"
            f"{kg_deal.car_line(deal)}\n\n"
            "Пришли по одному файлу:\n"
            "• СРТС\n"
            "• ID-карту продавца\n"
            "• доверенность (если уже есть)\n\n"
            "Дату ДКП напиши текстом (например «ДКП 12.10.2026»)." + have,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Отмена", callback_data=f"kgfile:cancel:{num}")
            ]]),
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
        # Пишем профиль через нормализацию: вместе с полями уедет и явный
        # тип счёта, а поля корреспондента у прямого счёта обнулятся — иначе
        # в сделке остались бы хвосты прежнего профиля. ИНН/КПП получателя
        # в журнал не пишутся: они в карточке компании, их берёт шаблон.
        ok = await agent.sheets.update_deal(
            contract_number, br.normalize(profile)
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
    # Фото документа из нескольких страниц: страницы копились, пока висело меню.
    pages = (context.user_data.pop("route_pages", None) or {}).get("pages")

    if route in ("new", "add", "sign") and pages and len(pages) > 1 and filepath:
        # Читать/подписывать можно только один файл — склеиваем страницы в PDF.
        try:
            out = str(Path(filepath).with_name(f"scan_{int(time.time())}_pages.pdf"))
            await asyncio.to_thread(images_to_pdf, pages, out)
            filepath, filename = out, "scan.pdf"
        except Exception as e:
            logger.error(f"Склейка страниц: {e}", exc_info=True)
            await query.edit_message_text(f"⚠️ Не удалось склеить страницы: {e}")
            return

    if route == "new":
        # Новая сделка — читаем документ с нуля. Файл прислан без кнопки
        # «Новая сделка», значит сделка прямая: брошенный выбор салона не
        # должен сделать её субагентской.
        salon.clear_pending(str(update.effective_chat.id))
        kg_deal.clear_new_pending(str(update.effective_chat.id))
        # С чистого листа: без этого LLM подставляла в новую сделку продавца,
        # машину и суммы из прошлой переписки. Сам файл уже у нас (filepath).
        memory.clear_history(str(update.effective_chat.id))
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
        # Скан в папку существующей сделки: номер → сводка → тип → PDF → статус
        # (тот же сценарий, что у «📎 Загрузить скан» в карточке сделки).
        context.user_data["awaiting_scan_for_existing"] = True
        context.user_data["existing_scan"] = {
            "filepath": filepath, "filename": filename,
            "pages": pages or ([filepath] if is_image_name(filename) else None),
            "ts": time.time(),
        }
        text = "Укажи номер сделки (например: `280626001`)"
        if is_image_name(filename):
            text += "\n_Если страниц несколько — пришли остальные фото, потом номер._"
        await query.edit_message_text(text + ":", parse_mode="Markdown",
                                      reply_markup=existing_prompt_keyboard(context))


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


async def on_kgfile(update, context, query, data):
    """Кнопки: callback_data «kgfile:…» — тип файла на этапе «🚗 Внести данные ТС».

    kgfile:<srts|id|poa|other> — файл из user_data["kg_file_pending"];
    kgfile:cancel:<номер> — выйти из ввода данных ТС.
    """
    chat_id = str(update.effective_chat.id)
    parts = data.split(":")
    if parts[1] == "cancel":
        kg_deal.clear_vehicle_pending(chat_id)
        context.user_data.pop("kg_file_pending", None)
        num = parts[2] if len(parts) > 2 else ""
        await query.edit_message_text(
            "Ввод данных ТС отменён.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ К сделке", callback_data=f"dealaction:{num}:menu")
            ]]) if num else None,
        )
        return

    kind = parts[1]
    pending = context.user_data.pop("kg_file_pending", None)
    veh = kg_deal.get_vehicle_pending(chat_id)
    if not pending or not veh:
        await query.edit_message_text("⚠️ Файл потерялся, пришлите его ещё раз.")
        return
    num = veh.get("num", "")
    if kind in kg_deal.FILE_KIND_PREFIX:
        kg_deal.add_vehicle_file(chat_id, pending["filepath"], pending["filename"], kind)
    label = kg_deal.FILE_KIND_LABELS.get(kind, "документ")
    await query.edit_message_text(f"📥 Читаю: {label}...")
    caption = (
        f"Сделка {num} («Учёт КГ», внесение данных ТС). Это {label}. "
        "Извлеки нужные данные. Если уже есть СРТС, данные продавца и дата ДКП — "
        "вызови set_kg_vehicle; иначе коротко скажи, чего ещё не хватает."
    )
    result = await typing_while(
        update.effective_chat.id, context,
        agent.process_file(pending["filepath"], pending["filename"], caption, chat_id=chat_id),
    )
    await send_result(query.message, result, context=context)
