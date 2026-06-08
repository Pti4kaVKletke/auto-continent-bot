import os
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
import openpyxl
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill


class DocumentBuilder:
    def __init__(self):
        self.templates_dir = Path(os.environ.get("TEMPLATES_DIR", "./templates"))
        self.output_dir = Path(tempfile.gettempdir()) / "tg_agent_docs"
        self.output_dir.mkdir(exist_ok=True)

    # ─── АГЕНТСКИЙ ДОГОВОР ────────────────────────────────────────────────

    async def build_contract(self, data: dict, number: str, date: str, commission_pct: float = 1.0) -> str:
        template = self.templates_dir / "contract_template.docx"
        if template.exists():
            return await self._fill_template(template, data, number, date,
                                             f"АГ_Договор_{number}", commission_pct)
        return await self._generate_contract(data, number, date, commission_pct)

    async def _generate_contract(self, data: dict, number: str, date: str, commission_pct: float = 1.0) -> str:
        doc = Document()
        self._setup_page(doc)

        t = doc.add_paragraph()
        t.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = t.add_run(f"АГЕНТСКИЙ ДОГОВОР № {number}")
        r.bold = True; r.font.size = Pt(13)

        s = doc.add_paragraph()
        s.alignment = WD_ALIGN_PARAGRAPH.CENTER
        s.add_run("на осуществление платежа в пользу третьего лица")

        doc.add_paragraph(f"г. Бишкек  «{date[:2]}» {self._month_name(date[3:5])} {date[6:]} г.")
        doc.add_paragraph()

        buyer = data.get("buyer_name", data.get("company_name", "____________"))
        doc.add_paragraph(
            f"ОсОО «Авто Континент», именуемое в дальнейшем «Агент», в лице "
            f"Генерального директора Колотовкина Ильи Валерьевича, действующего на основании Устава, "
            f"с одной стороны, и {buyer}, именуемый(ая) в дальнейшем «Принципал», "
            f"с другой стороны, заключили настоящий Агентский договор о нижеследующем:"
        )

        sections = [
            ("1. ПРЕДМЕТ ДОГОВОРА", [
                "1.1. Агент обязуется за вознаграждение совершить от своего имени, но за счёт "
                "Принципала действия по передаче денежных средств продавцу транспортного средства.",
                "1.2. Принципал перечисляет денежные средства Агенту безналичным путём, "
                "после чего Агент передаёт их Получателю наличными.",
            ]),
            ("2. ВОЗНАГРАЖДЕНИЕ АГЕНТА", [
                f"2.1. Вознаграждение Агента составляет {commission_pct}% от суммы перевода.",
                "2.2. Вознаграждение уплачивается одновременно с перечислением основной суммы.",
            ]),
            ("3. ОТВЕТСТВЕННОСТЬ СТОРОН", [
                "3.1. Стороны несут ответственность в соответствии с законодательством КР.",
                "3.2. Агент не несёт ответственности за качество приобретаемого ТС.",
            ]),
            ("4. РЕКВИЗИТЫ СТОРОН", [
                "Агент: ОсОО «Авто Континент», ИНН: 01905202610324, "
                "г. Бишкек, Октябрьский район, ул. Матросова, д. 58, Неж.Пом. 2",
                f"Принципал: {buyer}",
            ]),
        ]

        for title, items in sections:
            h = doc.add_paragraph()
            h.add_run(title).bold = True
            for item in items:
                doc.add_paragraph(item)

        doc.add_paragraph()
        self._add_signature_table(doc)

        path = self.output_dir / f"АГ_Договор_{number}.docx"
        doc.save(str(path))
        return str(path)

    # ─── ДКП ТС ───────────────────────────────────────────────────────────

    async def build_dkp(self, data: dict, number: str, date: str) -> str:
        template = self.templates_dir / "dkp_template.docx"
        if template.exists():
            return await self._fill_template(template, data, number, date, f"ДКП_ТС_{number}")
        return await self._generate_dkp(data, number, date)

    async def _generate_dkp(self, data: dict, number: str, date: str) -> str:
        doc = Document()
        self._setup_page(doc)

        t = doc.add_paragraph()
        t.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = t.add_run(f"ДОГОВОР КУПЛИ-ПРОДАЖИ ТРАНСПОРТНОГО СРЕДСТВА № {number}")
        r.bold = True; r.font.size = Pt(13)

        doc.add_paragraph(f"«{date[:2]}» {self._month_name(date[3:5])} {date[6:]} г.  г. Бишкек")
        doc.add_paragraph()

        seller   = data.get("seller_name", "____________")
        buyer    = data.get("buyer_name", data.get("company_name", "____________"))
        car      = data.get("car_model", "____________")
        vin      = data.get("car_vin", "____________")
        year     = data.get("car_year", "____")
        color    = data.get("car_color", "____________")
        price    = data.get("car_price", "____________")
        currency = data.get("currency", "RUB")

        doc.add_paragraph(
            f"Гражданин(ка) Кыргызской Республики {seller}, именуемый(ая) в дальнейшем «Продавец», "
            f"с одной стороны, и {buyer}, именуемый(ая) в дальнейшем «Покупатель», "
            f"с другой стороны, заключили настоящий Договор о нижеследующем:"
        )

        items = [
            f"1. Продавец передаёт в собственность Покупателя транспортное средство:\n"
            f"   Марка, модель: {car};\n"
            f"   Идентификационный номер (VIN): {vin};\n"
            f"   Год выпуска: {year};\n"
            f"   № кузова: {vin};\n"
            f"   Цвет: {color}.",
            f"2. Стоимость ТС составляет: {price} {currency}.",
            f"3. Со слов Продавца ТС никому не продано, не заложено, под арестом не состоит.",
            f"4. Покупатель производит оплату через платёжного агента — "
            f"ОсОО «Авто Континент» (ИНН: 01905202610324) — "
            f"в соответствии с Агентским договором № {number} от «{date}».",
            f"5. Право собственности переходит к Покупателю с момента подписания Договора.",
            f"6. Договор составлен в трёх экземплярах.",
        ]
        for item in items:
            doc.add_paragraph(item)

        doc.add_paragraph()
        self._add_signature_table(doc)

        path = self.output_dir / f"ДКП_ТС_{number}.docx"
        doc.save(str(path))
        return str(path)

    # ─── СЧЁТ (XLSX) ──────────────────────────────────────────────────────

    async def build_invoice(self, data: dict, number: str, date: str, commission_pct: float = 1.0) -> str:
        """
        Формирует счёт на оплату в формате, соответствующем шаблону компании.
        Структура: блок банковских реквизитов → заголовок → стороны → таблица → итоги → подписи.
        Колонки: A=№(4) B=описание(48) C=кол-во(8) D=ед.(6) E=цена(16) F=сумма(16)
        """
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Счёт"

        # ── Ширина колонок ────────────────────────────────────────────────
        ws.column_dimensions["A"].width = 4
        ws.column_dimensions["B"].width = 48
        ws.column_dimensions["C"].width = 8
        ws.column_dimensions["D"].width = 6
        ws.column_dimensions["E"].width = 16
        ws.column_dimensions["F"].width = 16

        # ── Стили ─────────────────────────────────────────────────────────
        bold        = Font(bold=True, size=10)
        bold_lg     = Font(bold=True, size=12)
        normal      = Font(size=10)
        small       = Font(size=8)
        center      = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left        = Alignment(horizontal="left",   vertical="center", wrap_text=True)
        left_top    = Alignment(horizontal="left",   vertical="top",    wrap_text=True)
        right       = Alignment(horizontal="right",  vertical="center", wrap_text=True)
        thin        = Side(style="thin")
        medium_side = Side(style="medium")
        brd         = Border(left=thin, right=thin, top=thin, bottom=thin)
        brd_med     = Border(left=medium_side, right=medium_side,
                             top=medium_side,  bottom=medium_side)
        fill_hdr    = PatternFill("solid", fgColor="DCE6F1")
        fill_gray   = PatternFill("solid", fgColor="F2F2F2")

        def cell(row, col, value="", font=None, align=None, border=None, fill=None, num_fmt=None):
            c = ws.cell(row=row, column=col, value=value)
            if font:   c.font   = font
            if align:  c.alignment = align
            if border: c.border = border
            if fill:   c.fill   = fill
            if num_fmt: c.number_format = num_fmt
            return c

        def merge(r1, c1, r2, c2, value="", font=None, align=None, border=None, fill=None):
            ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)
            c = ws.cell(row=r1, column=c1, value=value)
            if font:   c.font   = font
            if align:  c.alignment = align
            if border: c.border = border
            if fill:   c.fill   = fill
            return c

        # ── Данные ────────────────────────────────────────────────────────
        price_str = str(data.get("car_price", "0")).replace(" ", "").replace(",", ".")
        try:
            price_val = float(price_str)
        except Exception:
            price_val = 0.0

        commission  = round(price_val * commission_pct / 100, 2)
        total       = round(price_val + commission, 2)
        currency    = data.get("currency", "RUB")
        buyer       = data.get("buyer_name", data.get("company_name", ""))
        car         = (f"{data.get('car_model', '')} год выпуска {data.get('car_year', '')} "
                       f"VIN {data.get('car_vin', '')}").strip()

        # Дата прописью для заголовка: "02 июня 2026"
        day_n  = date[0:2]
        mon_n  = date[3:5]
        year_n = date[6:10]
        date_str = f"{day_n} {self._month_name(mon_n)} {year_n}"

        # Банковские реквизиты из data
        corr_name = data.get("bank_corr_line1", "")
        corr_bik  = data.get("bank_corr_line2", "")
        corr_acc  = data.get("bank_corr_line3", "")
        ben_name  = data.get("bank_ben_line1", "")
        ben_bik   = data.get("bank_ben_line2", "")
        acc_num   = data.get("account_number", "")
        acc_cur   = data.get("account_currency", currency)

        # ══ БЛОК 1: БАНКОВСКИЕ РЕКВИЗИТЫ (строки 1–9) ════════════════════
        r = 1
        # Банк-корреспондент
        merge(r, 1, r, 4, corr_name, font=normal, align=left)
        ws.row_dimensions[r].height = 15; r += 1
        merge(r, 1, r, 3, "БИК", font=small, align=left)
        cell(r, 4, corr_bik, font=normal, align=left)
        ws.row_dimensions[r].height = 14; r += 1
        merge(r, 1, r, 4, "Банк-корреспондент", font=small, align=left)
        ws.row_dimensions[r].height = 13; r += 1

        # Банк получателя
        merge(r, 1, r, 4, ben_name, font=normal, align=left)
        ws.row_dimensions[r].height = 15; r += 1
        merge(r, 1, r, 3, "БИК", font=small, align=left)
        cell(r, 4, ben_bik, font=normal, align=left)
        ws.row_dimensions[r].height = 14; r += 1

        merge(r, 1, r, 3, "Сч. №", font=small, align=left)
        cell(r, 4, corr_acc, font=normal, align=left)
        ws.row_dimensions[r].height = 14; r += 1

        merge(r, 1, r, 4, "Банк получателя", font=small, align=left)
        ws.row_dimensions[r].height = 13; r += 1

        # Получатель — Авто Континент
        merge(r, 1, r, 2, f"ИНН  01905202610324", font=normal, align=left)
        merge(r, 3, r, 3, "Сч. №", font=small, align=right)
        cell(r, 4, acc_num, font=normal, align=left)
        ws.row_dimensions[r].height = 15; r += 1

        merge(r, 1, r, 4, 'ОсОО "Авто Континент"', font=bold, align=left)
        ws.row_dimensions[r].height = 15; r += 1

        merge(r, 1, r, 4, "Получатель", font=small, align=left)
        ws.row_dimensions[r].height = 13; r += 1

        # Разделитель
        ws.row_dimensions[r].height = 4; r += 1

        # ══ БЛОК 2: ЗАГОЛОВОК ════════════════════════════════════════════
        merge(r, 1, r, 6,
              f"Счёт на оплату № {number} от {date_str} г.",
              font=bold_lg, align=center, border=brd_med)
        ws.row_dimensions[r].height = 28; r += 1

        # ══ БЛОК 3: СТОРОНЫ ══════════════════════════════════════════════
        merge(r, 1, r, 2, "Поставщик:\n(Исполнитель)", font=small, align=left_top, border=brd)
        merge(r, 3, r, 6,
              'ОсОО "Авто Континент", ИНН: 01905202610324, ОКПО: 34942535, '
              'Кыргызская Республика, г. Бишкек, Октябрьский район, ул. Матросова, д. 58, Неж.Пом. 2',
              font=normal, align=left_top, border=brd)
        ws.row_dimensions[r].height = 40; r += 1

        merge(r, 1, r, 2, "Покупатель:\n(Заказчик)", font=small, align=left_top, border=brd)
        merge(r, 3, r, 6, buyer, font=normal, align=left_top, border=brd)
        ws.row_dimensions[r].height = 22; r += 1

        # Разделитель
        ws.row_dimensions[r].height = 4; r += 1

        # ══ БЛОК 4: ТАБЛИЦА ══════════════════════════════════════════════
        # Заголовки
        hrow = r
        for col, (txt, w) in enumerate([
            ("№", 4), ("Товары (работы, услуги)", 48),
            ("Кол-во", 8), ("Ед.", 6), ("Цена", 16), ("Сумма", 16)
        ], 1):
            c = ws.cell(row=hrow, column=col, value=txt)
            c.font = bold; c.alignment = center
            c.border = brd; c.fill = fill_hdr
        ws.row_dimensions[hrow].height = 30; r += 1

        # Строка 1: оплата за авто
        item_desc = (f"Оплата по Агентскому договору {number} от {date_str} г. "
                     f"на оплату автомобиля {car}")
        num_fmt = '#,##0.00'
        for col, val in enumerate([1, item_desc, 1, "шт", price_val, price_val], 1):
            c = ws.cell(row=r, column=col, value=val)
            c.border = brd
            c.alignment = left_top if col == 2 else center
            c.font = normal
            if col in (5, 6): c.number_format = num_fmt
        ws.row_dimensions[r].height = 45; r += 1

        # Строка 2: комиссия
        for col, val in enumerate([2, f"Комиссия по Агентскому договору {number} от {date_str} г.",
                                    1, "шт", commission, commission], 1):
            c = ws.cell(row=r, column=col, value=val)
            c.border = brd
            c.alignment = left_top if col == 2 else center
            c.font = normal
            if col in (5, 6): c.number_format = num_fmt
        ws.row_dimensions[r].height = 28; r += 1

        # ══ БЛОК 5: ИТОГИ ════════════════════════════════════════════════
        for label, value, is_total in [
            ("Итого:",           total,  False),
            ("В том числе НДС:", "-",    False),
            ("Всего к оплате:",  total,  True),
        ]:
            merge(r, 1, r, 5, label,
                  font=bold if is_total else normal, align=right,
                  border=Border(right=thin, bottom=thin))
            c = ws.cell(row=r, column=6, value=value)
            c.font = bold if is_total else normal
            c.alignment = center
            c.border = brd
            if isinstance(value, float): c.number_format = num_fmt
            ws.row_dimensions[r].height = 18; r += 1

        # ══ БЛОК 6: ИТОГОВАЯ СТРОКА ══════════════════════════════════════
        ws.row_dimensions[r].height = 5; r += 1  # отступ

        total_fmt = f"{total:,.2f}".replace(",", " ")
        price_words = data.get("car_price_words", "")
        merge(r, 1, r, 6,
              f"Всего наименований 2, на сумму {total_fmt} {acc_cur}",
              font=bold, align=left)
        ws.row_dimensions[r].height = 18; r += 1

        merge(r, 1, r, 6, price_words, font=normal, align=left)
        ws.row_dimensions[r].height = 18; r += 1

        ws.row_dimensions[r].height = 8; r += 1  # отступ

        # ══ БЛОК 7: ПОДПИСИ ══════════════════════════════════════════════
        # Руководитель
        cell(r, 1, "Руководитель", font=normal, align=left)
        merge(r, 2, r, 3, "", border=Border(bottom=thin))   # место подписи
        merge(r, 4, r, 6, "Колотовкин Илья Валерьевич", font=normal, align=center)
        ws.row_dimensions[r].height = 22; r += 1

        merge(r, 2, r, 3, "подпись", font=small, align=center)
        merge(r, 4, r, 6, "расшифровка подписи", font=small, align=center)
        ws.row_dimensions[r].height = 12; r += 1

        ws.row_dimensions[r].height = 10; r += 1  # отступ

        # Бухгалтер
        cell(r, 1, "Бухгалтер", font=normal, align=left)
        merge(r, 2, r, 3, "", border=Border(bottom=thin))
        merge(r, 4, r, 6, "", border=Border(bottom=thin))
        ws.row_dimensions[r].height = 22; r += 1

        merge(r, 2, r, 3, "подпись", font=small, align=center)
        merge(r, 4, r, 6, "расшифровка подписи", font=small, align=center)
        ws.row_dimensions[r].height = 12; r += 1

        ws.row_dimensions[r].height = 10; r += 1
        merge(r, 2, r, 3, "М.П.", font=normal, align=center)
        ws.row_dimensions[r].height = 18

        # ── Параметры страницы ────────────────────────────────────────────
        ws.page_setup.orientation = "portrait"
        ws.page_setup.paperSize   = ws.PAPERSIZE_A4
        ws.page_margins.left      = 0.5
        ws.page_margins.right     = 0.3
        ws.page_margins.top       = 0.5
        ws.page_margins.bottom    = 0.5
        ws.print_area             = f"A1:F{r}"

        path = self.output_dir / f"Счёт_{number}.xlsx"
        wb.save(str(path))
        return str(path)

    # ─── КОНВЕРТАЦИЯ В PDF ────────────────────────────────────────────────

    async def convert_to_pdf(self, filepath: str) -> str | None:
        """
        Конвертирует файл в PDF через LibreOffice.
        Возвращает путь к PDF или None если LibreOffice недоступен.
        FIX: раньше возвращал исходный путь при неудаче — agent.py
        думал что PDF создан и пытался загрузить docx как PDF.
        """
        try:
            result = subprocess.run(
                ["libreoffice", "--headless", "--convert-to", "pdf",
                 "--outdir", str(self.output_dir), filepath],
                capture_output=True, timeout=60
            )
            pdf_path = str(filepath).rsplit(".", 1)[0] + ".pdf"
            if Path(pdf_path).exists() and Path(pdf_path).stat().st_size > 0:
                return pdf_path
        except FileNotFoundError:
            logger.warning("LibreOffice не установлен — PDF конвертация недоступна")
        except Exception as e:
            logger.error(f"Ошибка конвертации PDF: {e}")
        return None  # FIX: None вместо исходного пути — вызывающий код должен проверять

    # ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────

    def _setup_page(self, doc):
        section = doc.sections[0]
        section.page_width    = Cm(21)
        section.page_height   = Cm(29.7)
        section.left_margin   = Cm(3)
        section.right_margin  = Cm(1.5)
        section.top_margin    = Cm(2)
        section.bottom_margin = Cm(2)

    def _add_signature_table(self, doc):
        table = doc.add_table(rows=3, cols=2)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.cell(0, 0).text = "Продавец (Агент):"
        table.cell(0, 1).text = "Покупатель (Принципал):"
        table.cell(1, 0).text = "ОсОО «Авто Континент»"
        table.cell(2, 0).text = "\n_________________ / Колотовкин И.В. /"
        table.cell(2, 1).text = "\n_________________ /____________/"

    def _month_name(self, month_num: str) -> str:
        months = {
            "01": "января", "02": "февраля", "03": "марта",
            "04": "апреля", "05": "мая", "06": "июня",
            "07": "июля", "08": "августа", "09": "сентября",
            "10": "октября", "11": "ноября", "12": "декабря"
        }
        return months.get(month_num, month_num)

    async def _fill_template(self, template_path, data, number, date, output_name,
                              commission_pct: float = 1.0) -> str:
        from lxml import etree
        from copy import deepcopy

        doc = Document(str(template_path))

        day   = date[0:2]
        month = date[3:5]
        year  = date[6:10]

        price_str = data.get("car_price", "0").replace(" ", "").replace(",", ".")
        try:
            price_val = float(price_str)
            price_fmt = f"{price_val:,.0f}".replace(",", " ")
        except Exception:
            price_fmt = price_str
            price_val = 0

        # FIX: {{СУММА_НАЛИЧНЫМИ}} должна брать cash_amount (доллары для поручения),
        # а не car_price (рубли для ДКП). Раньше cash_fmt = f"{price_val:,.0f}" — НЕВЕРНО.
        cash_amount_raw = str(data.get("cash_amount", ""))
        try:
            cash_fmt = f"{float(cash_amount_raw.replace(' ', '')):,.0f}".replace(",", " ")
        except Exception:
            cash_fmt = cash_amount_raw

        replacements = {
            "{{НОМЕР}}":                    number,
            "{{ДЕНЬ}}":                     day,
            "{{МЕСЯЦ}}":                    self._month_name(month),
            "{{ГОД}}":                      year,
            "{{КОМИССИЯ}}":                 str(commission_pct),

            # Покупатель (гражданин РФ)
            "{{ПОКУПАТЕЛЬ_ФИО}}":           data.get("buyer_name", ""),
            "{{ПОКУПАТЕЛЬ_ДАТА_РОЖДЕНИЯ}}": data.get("buyer_birth_date", ""),
            "{{ПОКУПАТЕЛЬ_АДРЕС}}":         data.get("buyer_address", ""),
            "{{ПОКУПАТЕЛЬ_ИНИЦИАЛЫ}}":      data.get("buyer_initials", ""),
            "{{ПОКУПАТЕЛЬ_ПОЛНЫЕ_ДАННЫЕ}}": data.get("buyer_full_details", data.get("buyer_name", "")),

            # Паспорт покупателя (РФ)
            "{{ПАСПОРТ_СЕРИЯ}}":            data.get("passport_series", ""),
            "{{ПАСПОРТ_НОМЕР}}":            data.get("passport_number", ""),
            "{{ПАСПОРТ_ВЫДАН}}":            data.get("passport_issued_by", ""),
            "{{ПАСПОРТ_КОД}}":              data.get("passport_code", ""),
            "{{ПАСПОРТ_ДАТА_ВЫДАЧИ}}":      data.get("passport_issued_date", ""),

            # Продавец (гражданин КР)
            "{{ПРОДАВЕЦ_ФИО}}":             data.get("seller_name", ""),
            "{{ПРОДАВЕЦ_ДАТА_РОЖДЕНИЯ}}":   data.get("seller_birth_date", ""),
            "{{ПРОДАВЕЦ_АДРЕС}}":           data.get("seller_address", ""),
            "{{ПРОДАВЕЦ_ИНИЦИАЛЫ}}":        data.get("seller_initials", ""),
            "{{ПРОДАВЕЦ_ПОЛНЫЕ_ДАННЫЕ}}":   data.get("seller_full_details", data.get("seller_name", "")),

            # Идентификационная карта продавца (КР)
            # FIX: был data.get("seller_id") — но ключ в data всегда seller_id_number
            "{{ПРОДАВЕЦ_ID}}":              data.get("seller_id_number", data.get("seller_id", "")),
            "{{ПРОДАВЕЦ_ID_НОМЕР}}":        data.get("seller_id_number", data.get("seller_id", "")),
            "{{ПРОДАВЕЦ_ID_ВЫДАНА}}":       data.get("seller_id_issued_by", ""),
            "{{ПРОДАВЕЦ_ID_ДАТА}}":         data.get("seller_id_issued_date", ""),

            # Авто
            "{{МАРКА_МОДЕЛЬ}}":             data.get("car_model", ""),
            "{{VIN}}":                      data.get("car_vin", ""),
            "{{ГОД_ВЫП}}":                 data.get("car_year", ""),
            "{{ЦВЕТ}}":                     data.get("car_color", ""),
            "{{НОМ_КУЗОВА}}":              data.get("car_body_number", data.get("car_vin", "")),
            "{{НОМ_ТПО}}":                 data.get("tpo_number", ""),
            "{{ДЕНЬ_ТПО}}":                data.get("tpo_day", ""),
            "{{МЕС_ТПО}}":                 data.get("tpo_month", ""),
            "{{ГОД_ТПО}}":                 data.get("tpo_year", ""),

            # Цена и оплата
            "{{ЦЕНА_ЦИФРАМИ}}":            price_fmt,
            "{{ЦЕНА_ПРОПИСЬЮ}}":           data.get("car_price_words", ""),
            "{{ВАЛЮТА}}":                  data.get("currency", "рублей"),
            "{{СУММА_НАЛИЧНЫМИ}}":         cash_fmt,                              # FIX: теперь cash_amount
            # FIX: был {{СУММА_ПРОПИСЬЮ}} — не совпадало с плейсхолдером в шаблоне
            "{{СУММА_НАЛИЧНЫМИ_ПРОПИСЬЮ}}": data.get("cash_amount_words", ""),
            "{{ВАЛЮТА_НАЛИЧНЫМИ}}":        data.get("cash_currency", data.get("currency", "рублей")),

            # Банковские реквизиты
            "{{БАНК_КОРР_СТРОКА1}}":       data.get("bank_corr_line1", ""),
            "{{БАНК_КОРР_СТРОКА2}}":       data.get("bank_corr_line2", ""),
            "{{БАНК_КОРР_СТРОКА3}}":       data.get("bank_corr_line3", ""),
            "{{БАНК_ПОЛ_СТРОКА1}}":        data.get("bank_ben_line1", ""),
            "{{БАНК_ПОЛ_СТРОКА2}}":        data.get("bank_ben_line2", ""),
            "{{СЧЕТ_ВАЛЮТА}}":             data.get("account_currency", ""),
            "{{СЧЕТ_НОМЕР}}":              data.get("account_number", ""),
        }

        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

        def replace_in_para(para):
            """
            FIX: Заменяем плейсхолдеры через XML напрямую.

            Проблема старого кода: _apply_replacements_to_para() запускалась первой
            и меняла runs[0].text. После этого _process_paragraph_xml() не находила
            плейсхолдеров (они уже заменены) и ничего не делала — XML-метод был мёртвым.
            Но если _apply_replacements_to_para() не срабатывала (Word дробил {{НОМЕР}}
            на несколько runs с разным форматированием), текст вообще не заменялся.

            Решение: только XML-метод. Он:
            1. Собирает полный текст из ВСЕХ w:r → w:t элементов
            2. Выполняет замену
            3. Удаляет все старые runs
            4. Вставляет один новый run с нужным текстом НА МЕСТО первого удалённого
               (не в конец параграфа через SubElement!)
            """
            p_elem = para._element
            runs = p_elem.findall(f"{{{W}}}r")
            if not runs:
                return

            # Полный текст параграфа (объединяем все w:t внутри w:r)
            full_text = "".join(
                t.text or ""
                for r in runs
                for t in r.findall(f"{{{W}}}t")
            )
            if not any(ph in full_text for ph in replacements):
                return

            # Запоминаем форматирование и позицию первого run
            first_rpr = runs[0].find(f"{{{W}}}rPr")
            children = list(p_elem)
            insert_idx = children.index(runs[0])

            # Выполняем замену
            new_text = full_text
            for ph, val in replacements.items():
                new_text = new_text.replace(ph, str(val) if val is not None else "")

            # Удаляем все старые runs
            for r in runs:
                p_elem.remove(r)

            # Создаём новый run
            new_run = etree.Element(f"{{{W}}}r")
            if first_rpr is not None:
                new_run.append(deepcopy(first_rpr))
            new_t = etree.SubElement(new_run, f"{{{W}}}t")
            new_t.text = new_text
            if new_text and (new_text[0] == " " or new_text[-1] == " "):
                new_t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

            # FIX: вставляем на место первого удалённого run, а не в конец (SubElement)
            p_elem.insert(insert_idx, new_run)

        # Основной текст документа
        for para in doc.paragraphs:
            replace_in_para(para)

        # Таблицы
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        replace_in_para(para)

        # Колонтитулы
        for section in doc.sections:
            for para in section.header.paragraphs:
                replace_in_para(para)
            for para in section.footer.paragraphs:
                replace_in_para(para)

        path = self.output_dir / f"{output_name}.docx"
        doc.save(str(path))
        return str(path)
