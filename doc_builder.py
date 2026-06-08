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

        buyer = data.get("company_name", "____________")
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
                f"Принципал: {buyer}" +
                (f", ИНН: {data['inn']}" if data.get("inn") else "") +
                (f", адрес: {data['address']}" if data.get("address") else ""),
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

        seller = data.get("seller_name", "____________")
        buyer  = data.get("company_name", "____________")
        car    = data.get("car_model", "____________")
        vin    = data.get("car_vin", "____________")
        year   = data.get("car_year", "____")
        color  = data.get("car_color", "____________")
        price  = data.get("car_price", "____________")
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
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Счёт"

        ws.column_dimensions["A"].width = 5
        ws.column_dimensions["B"].width = 50
        ws.column_dimensions["C"].width = 10
        ws.column_dimensions["D"].width = 8
        ws.column_dimensions["E"].width = 18
        ws.column_dimensions["F"].width = 18

        bold = Font(bold=True)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left = Alignment(horizontal="left", vertical="center", wrap_text=True)
        thin = Side(style="thin")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        header_fill = PatternFill("solid", fgColor="D9E1F2")

        ws.merge_cells("A1:F1")
        ws["A1"] = f"СЧЁТ НА ОПЛАТУ № {number} от {date} г."
        ws["A1"].font = Font(bold=True, size=13)
        ws["A1"].alignment = center
        ws.row_dimensions[1].height = 30

        ws.merge_cells("A2:F2")
        ws["A2"] = "Поставщик: ОсОО «Авто Континент», ИНН: 01905202610324, г. Бишкек, ул. Матросова 58"
        ws["A2"].alignment = left
        ws.row_dimensions[2].height = 20

        ws.merge_cells("A3:F3")
        ws["A3"] = f"Покупатель: {data.get('company_name', '')}"
        ws["A3"].alignment = left
        ws.row_dimensions[3].height = 20

        headers = ["№", "Наименование товара (работы, услуги)", "Кол-во", "Ед.", "Цена", "Сумма"]
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=5, column=col, value=h)
            cell.font = bold
            cell.alignment = center
            cell.border = border
            cell.fill = header_fill
        ws.row_dimensions[5].height = 30

        car = f"{data.get('car_model', 'Автомобиль')} {data.get('car_year', '')} VIN: {data.get('car_vin', '')}".strip()
        price_str = data.get("car_price", "0").replace(" ", "").replace(",", ".")
        try:
            price_val = float(price_str)
        except Exception:
            price_val = 0

        currency = data.get("currency", "RUB")
        row_data = ["1", f"Оплата по Агентскому договору № {number} — {car}", "1", "шт.", price_val, price_val]
        for col, val in enumerate(row_data, 1):
            cell = ws.cell(row=6, column=col, value=val)
            cell.border = border
            cell.alignment = center if col != 2 else left
        ws.row_dimensions[6].height = 40

        commission = round(price_val * commission_pct / 100, 2)
        comm_data = ["2", f"Комиссия {commission_pct}% по Агентскому договору № {number}", "1", "шт.", commission, commission]
        for col, val in enumerate(comm_data, 1):
            cell = ws.cell(row=7, column=col, value=val)
            cell.border = border
            cell.alignment = center if col != 2 else left
        ws.row_dimensions[7].height = 30

        total = price_val + commission
        ws.merge_cells("A9:E9")
        ws["A9"] = "ИТОГО К ОПЛАТЕ:"
        ws["A9"].font = bold
        ws["A9"].alignment = Alignment(horizontal="right")
        ws["F9"] = total
        ws["F9"].font = bold
        ws["F9"].border = border

        ws.merge_cells("A10:F10")
        ws["A10"] = f"Валюта: {currency}"
        ws["A10"].alignment = left

        ws.merge_cells("A12:C12")
        ws["A12"] = "Руководитель: Колотовкин Илья Валерьевич"
        ws.merge_cells("D12:F12")
        ws["D12"] = "Подпись: ________________"

        path = self.output_dir / f"Счёт_{number}.xlsx"
        wb.save(str(path))
        return str(path)

    # ─── КОНВЕРТАЦИЯ В PDF ────────────────────────────────────────────────

    async def convert_to_pdf(self, filepath: str) -> str:
        try:
            result = subprocess.run(
                ["libreoffice", "--headless", "--convert-to", "pdf",
                 "--outdir", str(self.output_dir), filepath],
                capture_output=True, timeout=60
            )
            pdf_path = str(filepath).rsplit(".", 1)[0] + ".pdf"
            if Path(pdf_path).exists():
                return pdf_path
        except Exception as e:
            print(f"Ошибка конвертации PDF: {e}")
        return filepath

    # ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────

    def _setup_page(self, doc):
        section = doc.sections[0]
        section.page_width  = Cm(21)
        section.page_height = Cm(29.7)
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
        doc = Document(str(template_path))

        # Разбираем дату ДД.ММ.ГГГГ
        day   = date[0:2]
        month = date[3:5]
        year  = date[6:10]

        # Цена
        price_str = data.get("car_price", "0").replace(" ", "").replace(",", ".")
        try:
            price_val = float(price_str)
            price_fmt = f"{price_val:,.0f}".replace(",", " ")
        except Exception:
            price_fmt = price_str
            price_val = 0

        commission = round(price_val * commission_pct / 100, 2)
        cash_amount = price_val
        cash_fmt = f"{cash_amount:,.0f}".replace(",", " ")

        replacements = {
            "{{НОМЕР}}":                    number,
            "{{ДЕНЬ}}":                     day,
            "{{МЕСЯЦ}}":                    self._month_name(month),
            "{{ГОД}}":                      year,
            "{{КОМИССИЯ}}":                 str(commission_pct),

            # Покупатель (гражданин РФ)
            "{{ПОКУПАТЕЛЬ_ФИО}}":           data.get("buyer_name", ""),
            "{{ПАСПОРТ_СЕРИЯ}}":            data.get("passport_series", ""),
            "{{ПАСПОРТ_НОМЕР}}":            data.get("passport_number", ""),
            "{{ПАСПОРТ_ВЫДАН}}":            data.get("passport_issued_by", ""),
            "{{ПАСПОРТ_КОД}}":              data.get("passport_code", ""),
            "{{ПОКУПАТЕЛЬ_ПОЛНЫЕ_ДАННЫЕ}}": data.get("buyer_full_details", data.get("buyer_name", "")),
            "{{ПОКУПАТЕЛЬ_ИНИЦИАЛЫ}}":      data.get("buyer_initials", ""),

            # Продавец (гражданин КР)
            "{{ПРОДАВЕЦ_ФИО}}":             data.get("seller_name", ""),
            "{{ПРОДАВЕЦ_ID}}":              data.get("seller_id", ""),
            "{{ПРОДАВЕЦ_ПОЛНЫЕ_ДАННЫЕ}}":   data.get("seller_full_details", data.get("seller_name", "")),
            "{{ПРОДАВЕЦ_ИНИЦИАЛЫ}}":        data.get("seller_initials", ""),

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
            "{{СУММА_НАЛИЧНЫМИ}}":         cash_fmt,
            "{{СУММА_ПРОПИСЬЮ}}":          data.get("cash_amount_words", data.get("car_price_words", "")),
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

        def _merge_runs_text(para) -> str:
            return "".join(run.text for run in para.runs)

        def _apply_replacements_to_para(para):
            if not para.runs:
                return
            full_text = _merge_runs_text(para)
            new_text = full_text
            for ph, val in replacements.items():
                if ph in new_text:
                    new_text = new_text.replace(ph, str(val) if val is not None else "")
            if new_text == full_text:
                return
            para.runs[0].text = new_text
            for run in para.runs[1:]:
                run.text = ""

        def _process_paragraph_xml(para):
            """XML-метод для случаев когда Word разбил плейсхолдер между runs."""
            from lxml import etree
            from copy import deepcopy

            W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            p_elem = para._element
            runs = p_elem.findall(f"{{{W}}}r")
            if not runs:
                return

            full_text = "".join(
                t.text or ""
                for r in runs
                for t in r.findall(f"{{{W}}}t")
            )
            has_placeholder = any(ph in full_text for ph in replacements)
            if not has_placeholder:
                return

            first_rpr = runs[0].find(f"{{{W}}}rPr")

            new_text = full_text
            for ph, val in replacements.items():
                if ph in new_text:
                    new_text = new_text.replace(ph, str(val) if val is not None else "")

            for r in runs:
                p_elem.remove(r)

            new_run = etree.SubElement(p_elem, f"{{{W}}}r")
            if first_rpr is not None:
                new_run.insert(0, deepcopy(first_rpr))
            new_t = etree.SubElement(new_run, f"{{{W}}}t")
            new_t.text = new_text
            if new_text.startswith(" ") or new_text.endswith(" "):
                new_t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

        def process_para(para):
            _apply_replacements_to_para(para)
            _process_paragraph_xml(para)

        # Параграфы документа
        for para in doc.paragraphs:
            process_para(para)

        # Таблицы
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        process_para(para)

        # Колонтитулы
        for section in doc.sections:
            for para in section.header.paragraphs:
                process_para(para)
            for para in section.footer.paragraphs:
                process_para(para)

        path = self.output_dir / f"{output_name}.docx"
        doc.save(str(path))
        return str(path)
