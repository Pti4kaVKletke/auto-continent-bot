"""sign_ui.py — сценарий «подписать чужой документ» в боте.

Логика поиска места и вставки картинок живёт в `sign_pdf`, здесь только
разговор с человеком: принять файл, показать, куда встали подпись и печать,
дать поправить и отдать готовый PDF.

Порядок намеренно такой: бот СРАЗУ ставит подпись и показывает результат
картинкой, а не спрашивает подтверждение на пустую рамку. Разметка договоров
у контрагентов произвольная, и увидеть готовый лист — единственный надёжный
способ понять, туда ли попало. Кнопка «Готово» после этого нужна одним
нажатием, а «Поправить» — на те случаи, когда бот ошибся.

Состояние одного разбора лежит в `context.user_data["sign"]`, ключи:
    pdf       — путь к исходнику, уже приведённому к PDF
    name      — имя файла, как его прислали (для имени результата)
    key       — ключ розыгрыша; фиксируется один раз, поэтому предпросмотр
                и итоговый файл получают ОДИН И ТОТ ЖЕ росчерк и оттиск
    slots     — все найденные места
    on        — множество индексов мест, которые реально подписываем
    cur       — место, открытое в режиме правки
"""
import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

import company
import sign_pdf

logger = logging.getLogger(__name__)

# Куда на Drive складываются подписанные документы. Папка одна на всё:
# документы контрагентов к конкретной сделке чаще всего не привязаны.
DRIVE_FOLDER = "Подписанные документы"

STEP_MM = 5.0          # шаг стрелок при правке
MAX_SLOTS_SHOWN = 8    # больше в меню всё равно не нужно


def clear_state(context):
    context.user_data.pop("sign", None)


def _hints(getter):
    """Строки, по которым узнаём СВОЙ блок реквизитов в чужом документе."""
    c = company.card(getter)
    out = [c.get("company_name", ""), c.get("company_inn", ""),
           c.get("company_inn_rf", ""), c.get("company_okpo", "")]
    # Фамилия директора без инициалов: в чужих договорах она встречается и
    # как «Колотовкин И.В.», и как «Колотовкина Ильи Валерьевича».
    name = (c.get("director_name") or "").split()
    if name:
        out.append(name[0])
    for extra in ("company_name_full",):
        if c.get(extra):
            out.append(c[extra])
    return [s.strip() for s in out if s and len(s.strip()) > 3]


# ─── Шаг 1: приняли файл ────────────────────────────────────────────────

async def start(message, context, filepath: str, filename: str,
                builder, getter=None):
    """Принять документ и показать первый результат."""
    clear_state(context)
    ext = Path(filename).suffix.lower()

    if ext in (".pdf",):
        pdf = filepath
    elif ext in (".docx", ".doc", ".xlsx", ".xls", ".odt", ".rtf"):
        note = await message.reply_text("📄 Перевожу в PDF…")
        # convert_to_pdf кладёт результат в свою папку и возвращает путь,
        # собранный из ИМЕНИ ИСХОДНИКА — файл должен лежать там же.
        src = builder.output_dir / Path(filepath).name
        if str(src) != str(filepath):
            src.write_bytes(Path(filepath).read_bytes())
        pdf = await builder.convert_to_pdf(str(src))
        await note.delete()
        if not pdf:
            await message.reply_text(
                "❌ Не смогла перевести файл в PDF. Пришли его сразу PDF-ом.")
            return
    else:
        await message.reply_text(
            "❌ Подписать могу PDF или Word. Картинку и скан пока не умею — "
            "в них нет текста, по которому я нахожу место подписи.")
        return

    try:
        slots = sign_pdf.find_slots(pdf, hints=_hints(getter))
    except Exception as e:
        logger.error(f"sign_ui: поиск мест не удался: {e}", exc_info=True)
        await message.reply_text(f"❌ Не смогла разобрать документ: {e}")
        return

    if not slots:
        await message.reply_text(
            "🤷 Не нашла в документе места для подписи — ни линии, ни «М.П.».\n"
            "Если это скан, текста в нём нет и искать не по чему.")
        return

    # Работаем по индексам, а не по самим местам: два места в документе
    # вполне могут оказаться одинаковыми словарями, и .index() вернёт не то.
    ours = [i for i, s in enumerate(slots) if s["our"]]
    if not ours:
        # Линии есть, но наших реквизитов рядом нет. Подписывать вслепую
        # чужую колонку нельзя — берём лучшее по счёту и показываем.
        ours = [0]

    context.user_data["sign"] = {
        "pdf": pdf,
        "name": filename,
        "key": f"{Path(filename).stem}_{os.path.basename(pdf)}",
        "slots": slots,
        "on": set(ours),
        "cur": ours[0],
    }
    await _show(message, context, edit=False)


# ─── Показ ──────────────────────────────────────────────────────────────

def _selected(st):
    return [st["slots"][i] for i in sorted(st["on"])]


def _label(st, i):
    """Подпись места для меню. В двухколоночном блоке реквизитов заголовок
    над обеими колонками один и тот же («Генеральный директор»), и без
    стороны две строки в списке неразличимы."""
    s = st["slots"][i]
    same = [j for j, o in enumerate(st["slots"])
            if j != i and o["page"] == s["page"] and o["label"] == s["label"]]
    if not same:
        return s["label"]
    side = "левая" if s["col"] == "left" else "правая"
    return f"{s['label']} · {side} колонка"


def _build(st, out_path):
    """Собрать PDF с текущими местами. Ключ розыгрыша один на весь разбор,
    поэтому предпросмотр и итог выглядят одинаково."""
    return sign_pdf.sign(st["pdf"], out_path, _selected(st), st["key"])


def _preview(st, page_no=None):
    """PNG страницы с уже поставленными подписью и печатью."""
    tmp = Path(st["pdf"]).with_name("_sign_preview.pdf")
    _build(st, str(tmp))
    if page_no is None:
        sel = _selected(st)
        page_no = sel[0]["page"] if sel else 0
    return sign_pdf.render_page(str(tmp), page_no, dpi=130), str(tmp)


def _plural(n, one, few, many):
    """«1 место», «2 места», «5 мест» — в списке мест это видно каждый раз."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def _overview_text(st):
    sel = _selected(st)
    if not sel:
        return "Ни одного места не выбрано. Нажми «Поправить» и включи нужное."
    lines = [f"✍️ *{st['name']}*", ""]
    lines.append(f"Подписываю в {len(sel)} "
                 f"{_plural(len(sel), 'месте', 'местах', 'местах')}:")
    for i in sorted(st["on"]):
        s = st["slots"][i]
        mark = "по М.П." if s.get("mp") else "по линии подписи"
        lines.append(f"• {_label(st, i)} — {mark}")
    if len(sel) < len(st["slots"]):
        rest = len(st["slots"]) - len(sel)
        lines.append(
            f"\nЕщё {rest} {_plural(rest, 'похожее место', 'похожих места', 'похожих мест')} "
            f"пропускаю — {'оно' if rest == 1 else 'они'} в чужой колонке.")
    lines.append("\nНиже — как это выглядит. Проверь и подтверди.")
    return "\n".join(lines)


def _overview_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово, отдай PDF", callback_data="sign:ok")],
        [InlineKeyboardButton("✏️ Поправить", callback_data="sign:edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="sign:cancel")],
    ])


async def _show(message, context, edit=True):
    st = context.user_data.get("sign")
    if not st:
        return
    png, _ = _preview(st)
    caption = _overview_text(st)
    if edit:
        await message.edit_media(
            InputMediaPhoto(png, caption=caption, parse_mode="Markdown"),
            reply_markup=_overview_kb())
    else:
        await message.reply_photo(png, caption=caption, parse_mode="Markdown",
                                  reply_markup=_overview_kb())


def _place_kb(st, i):
    s = st["slots"][i]
    on = i in st["on"]
    rows = [
        [InlineKeyboardButton("⬆️", callback_data=f"sign:mv:{i}:0:-1"),
         InlineKeyboardButton("⬇️", callback_data=f"sign:mv:{i}:0:1")],
        [InlineKeyboardButton("⬅️", callback_data=f"sign:mv:{i}:-1:0"),
         InlineKeyboardButton("➡️", callback_data=f"sign:mv:{i}:1:0")],
        [InlineKeyboardButton("🚫 Не подписывать здесь" if on else "✅ Подписать здесь",
                              callback_data=f"sign:tog:{i}")],
        [InlineKeyboardButton("◀️ К общему виду", callback_data="sign:back")],
    ]
    if len(st["slots"]) > 1:
        rows.insert(3, [InlineKeyboardButton("📄 Другое место",
                                             callback_data="sign:edit")])
    return InlineKeyboardMarkup(rows), s


async def _show_place(message, context, i):
    st = context.user_data["sign"]
    st["cur"] = i
    kb, s = _place_kb(st, i)
    png, _ = _preview(st, page_no=s["page"])
    text = (f"*{_label(st, i)}*\n"
            f"{'подписываю' if i in st['on'] else 'пропускаю'} · "
            f"{'печать по М.П.' if s.get('mp') else 'печать по линии'}\n\n"
            f"Стрелки двигают на {STEP_MM:.0f} мм.")
    await message.edit_media(
        InputMediaPhoto(png, caption=text, parse_mode="Markdown"),
        reply_markup=kb)


# ─── Кнопки ─────────────────────────────────────────────────────────────

async def handle_callback(update, context, data: str, drive=None) -> bool:
    """True, если кнопка относится к подписанию."""
    if not data.startswith("sign:"):
        return False
    query = update.callback_query
    st = context.user_data.get("sign")
    if not st:
        await query.edit_message_caption("⌛ Разбор устарел. Пришли документ заново.")
        return True

    parts = data.split(":")
    action = parts[1]

    if action == "cancel":
        clear_state(context)
        await query.edit_message_caption("Отменила. Документ не подписан.")
        return True

    if action == "back":
        await _show(query.message, context)
        return True

    if action == "edit":
        rows = []
        for i, s in enumerate(st["slots"][:MAX_SLOTS_SHOWN]):
            tick = "✅" if i in st["on"] else "▫️"
            rows.append([InlineKeyboardButton(f"{tick} {_label(st, i)}",
                                              callback_data=f"sign:pick:{i}")])
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="sign:back")])
        await query.edit_message_caption(
            "Какое место поправить?\n"
            "✅ — подписываю, ▫️ — пропускаю.",
            reply_markup=InlineKeyboardMarkup(rows))
        return True

    if action == "pick":
        await _show_place(query.message, context, int(parts[2]))
        return True

    if action == "tog":
        i = int(parts[2])
        st["on"].symmetric_difference_update({i})
        await _show_place(query.message, context, i)
        return True

    if action == "mv":
        i, dx, dy = int(parts[2]), float(parts[3]), float(parts[4])
        st["slots"][i] = sign_pdf.nudge(st["slots"][i],
                                        dx * STEP_MM, dy * STEP_MM)
        await _show_place(query.message, context, i)
        return True

    if action == "ok":
        if not st["on"]:
            await query.answer("Не выбрано ни одного места", show_alert=True)
            return True
        await query.edit_message_caption("📎 Собираю файл…")
        name = f"{Path(st['name']).stem}_подписано.pdf"
        out = str(Path(st["pdf"]).with_name(name))
        try:
            _build(st, out)
        except Exception as e:
            logger.error(f"sign_ui: сборка не удалась: {e}", exc_info=True)
            await query.edit_message_caption(f"❌ Не смогла собрать файл: {e}")
            return True

        with open(out, "rb") as f:
            await query.message.reply_document(f, filename=name)

        link = ""
        if drive is not None:
            try:
                folder = await drive._get_or_create_folder(
                    DRIVE_FOLDER, drive.root_folder_id)
                link = await drive.upload_file(out, name, folder)
            except Exception as e:
                logger.warning(f"sign_ui: не смогла загрузить на Drive: {e}")
        if link:
            await query.message.reply_text(
                f"✅ Готово. [Копия в папке «{DRIVE_FOLDER}»]({link})",
                parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await query.message.reply_text("✅ Готово. Файл выше.")
        clear_state(context)
        return True

    return True
