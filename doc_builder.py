import os
import asyncio
import logging
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

logger = logging.getLogger(__name__)


# ─── Сумма прописью (рубли) ────────────────────────────────────────────────

_UNITS = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_UNITS_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
          "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят",
         "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот",
             "шестьсот", "семьсот", "восемьсот", "девятьсот"]

# (ед.ч., мн.ч. 2-4, мн.ч. 5+, женский род)
_SCALE = [
    ("", "", "", False),
    ("тысяча", "тысячи", "тысяч", True),
    ("миллион", "миллиона", "миллионов", False),
    ("миллиард", "миллиарда", "миллиардов", False),
]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n100 = n % 100
    n10 = n % 10
    if 11 <= n100 <= 14:
        return many
    if n10 == 1:
        return one
    if 2 <= n10 <= 4:
        return few
    return many


def _three_digits_to_words(n: int, feminine: bool = False) -> str:
    words = []
    h, rem = divmod(n, 100)
    if h:
        words.append(_HUNDREDS[h])
    t, u = divmod(rem, 10)
    if t == 1:
        words.append(_TEENS[u])
    else:
        if t:
            words.append(_TENS[t])
        if u:
            words.append((_UNITS_F if feminine else _UNITS)[u])
    return " ".join(words)


def amount_to_words_rub(amount) -> str:
    """
    Преобразует сумму в рублях в строку прописью с копейками.
    Пример: 3997500 -> "Три миллиона девятьсот девяносто семь тысяч пятьсот рублей 00 копеек"
    """
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return ""

    rub = int(amount)
    kop = round((amount - rub) * 100)

    if rub == 0:
        rub_words = "ноль"
    else:
        groups = []
        n = rub
        scale_idx = 0
        while n > 0:
            n, group = divmod(n, 1000)
            if group:
                groups.append((group, scale_idx))
            scale_idx += 1

        parts = []
        for group, idx in reversed(groups):
            one, few, many, feminine = _SCALE[idx]
            parts.append(_three_digits_to_words(group, feminine=feminine))
            if idx > 0:
                parts.append(_plural(group, one, few, many))
        rub_words = " ".join(p for p in parts if p)

    rub_words = rub_words[0].upper() + rub_words[1:]
    rub_label = _plural(rub, "рубль", "рубля", "рублей")
    kop_label = _plural(kop, "копейка", "копейки", "копеек")

    return f"{rub_words} {rub_label} {kop:02d} {kop_label}"


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

        doc.add_paragraph(f"г. Бишкек «{date[:2]}» {self._month_name(date[3:5])} {date[6:]} г.")
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

        doc.add_paragraph(f"«{date[:2]}» {self._month_name(date[3:5])} {date[6:]} г. г. Бишкек")
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
        Формирует счёт на оплату из шаблона invoice_template.xlsx
        (содержит реальную печать и подпись директора).
        """
        template = self.templates_dir / "invoice_template.xlsx"
        wb = openpyxl.load_workbook(str(template))
        ws = wb.active

        price_str = str(data.get("car_price", "0")).replace(" ", "").replace(",", ".")
        try:
            price_val = float(price_str)
        except Exception:
            price_val = 0.0

        commission = round(price_val * commission_pct / 100, 2)
        total       = round(price_val + commission, 2)
        currency    = data.get("currency", "RUB")
        acc_cur     = data.get("account_currency", currency)
        buyer       = data.get("buyer_name", data.get("company_name", ""))
        car         = (f"{data.get('car_model', '')} год выпуска {data.get('car_year', '')} "
                       f"VIN {data.get('car_vin', '')}").strip()

        day_n  = date[0:2]
        mon_n  = date[3:5]
        year_n = date[6:10]
        date_str = f"{day_n} {self._month_name(mon_n)} {year_n}"

        total_fmt   = f"{total:,.2f}".replace(",", " ")
        total_words = amount_to_words_rub(total) if acc_cur == "RUB" else ""

        replacements = {
            "{{BANK_CORR_NAME}}":    data.get("bank_corr_line1", ""),
            "{{BANK_CORR_BIK}}":     data.get("bank_corr_line2", ""),
            "{{BANK_CORR_ACC}}":     data.get("bank_corr_line3", ""),
            "{{BANK_BEN_NAME}}":     data.get("bank_ben_line1", ""),
            "{{BANK_BEN_LINE2}}":    data.get("bank_ben_line2", ""),
            "{{BANK_BEN_INN}}":      "01905202610324",
            "{{ACCOUNT_NUMBER}}":    data.get("account_number", ""),
        }

        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str):
                    for ph, val in replacements.items():
                        if ph in c.value:
                            c.value = c.value.replace(ph, str(val))

        # Заголовок счёта
        ws["B16"] = f"Счет на оплату № {number} от {date_str} г."

        # Покупатель
        ws["G22"] = buyer

        # Таблица: позиция автомобиля
        ws["D25"] = f"Оплата по Агентскому договору {number} от {date_str} г. на оплату автомобиля {car}"
        ws["Z25"] = price_val

        # Комиссия
        ws["D26"] = f"Комиссия по Агентскому договору {number} от {date_str} г."
        ws["Z26"] = commission

        # Итоговая строка и сумма прописью
        ws["B31"] = f"Всего наименований 2, на сумму {total_fmt} {acc_cur}"
        ws["B32"] = total_words

        path = self.output_dir / f"Счёт_{number}.xlsx"
        wb.save(str(path))
        return str(path)

    # ─── КОНВЕРТАЦИЯ В PDF ────────────────────────────────────────────────

    async def convert_to_pdf(self, filepath: str) -> str | None:
        """
        Конвертирует файл в PDF через LibreOffice.
        ИСПРАВЛЕНО: asyncio.create_subprocess_exec вместо subprocess.run —
        не блокирует event loop Telegram-бота.
        Возвращает путь к PDF или None если LibreOffice недоступен / ошибка.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "libreoffice", "--headless", "--convert-to", "pdf",
                "--outdir", str(self.output_dir), filepath,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
                if stderr:
                    logger.debug(f"LibreOffice stderr: {stderr.decode(errors='ignore')}")
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.warning("LibreOffice: таймаут конвертации (> 60 сек)")
                return None

            pdf_path = str(filepath).rsplit(".", 1)[0] + ".pdf"
            if Path(pdf_path).exists() and Path(pdf_path).stat().st_size > 0:
                return pdf_path

            logger.warning(f"LibreOffice: PDF не создан для {filepath}")
            return None

        except FileNotFoundError:
            logger.warning("LibreOffice не установлен — PDF конвертация недоступна")
        except Exception as e:
            logger.error(f"Ошибка конвертации PDF: {e}")
        return None

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
            "01": "января",  "02": "февраля", "03": "марта",
            "04": "апреля",  "05": "мая",     "06": "июня",
            "07": "июля",    "08": "августа", "09": "сентября",
            "10": "октября", "11": "ноября",  "12": "декабря",
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

        cash_amount_raw = str(data.get("cash_amount", ""))
        try:
            cash_fmt = f"{float(cash_amount_raw.replace(' ', '')):,.0f}".replace(",", " ")
        except Exception:
            cash_fmt = cash_amount_raw

        replacements = {
            "{{НОМЕР}}":   number,
            "{{ДЕНЬ}}":    day,
            "{{МЕСЯЦ}}":   self._month_name(month),
            "{{ГОД}}":     year,
            "{{КОМИССИЯ}}": str(commission_pct),

            # Покупатель (гражданин РФ)
            "{{ПОКУПАТЕЛЬ_ФИО}}":           data.get("buyer_name", ""),
            "{{ПОКУПАТЕЛЬ_ДАТА_РОЖДЕНИЯ}}": data.get("buyer_birth_date", ""),
            "{{ПОКУПАТЕЛЬ_АДРЕС}}":         data.get("buyer_address", ""),
            "{{ПОКУПАТЕЛЬ_ИНИЦИАЛЫ}}":      data.get("buyer_initials", ""),
            "{{ПОКУПАТЕЛЬ_ПОЛНЫЕ_ДАННЫЕ}}": data.get("buyer_full_details", data.get("buyer_name", "")),

            # Паспорт покупателя (РФ)
            "{{ПАСПОРТ_СЕРИЯ}}":       data.get("passport_series", ""),
            "{{ПАСПОРТ_НОМЕР}}":       data.get("passport_number", ""),
            "{{ПАСПОРТ_ВЫДАН}}":       data.get("passport_issued_by", ""),
            "{{ПАСПОРТ_КОД}}":         data.get("passport_code", ""),
            "{{ПАСПОРТ_ДАТА_ВЫДАЧИ}}": data.get("passport_issued_date", ""),

            # Продавец (гражданин КР)
            "{{ПРОДАВЕЦ_ФИО}}":           data.get("seller_name", ""),
            "{{ПРОДАВЕЦ_ДАТА_РОЖДЕНИЯ}}": data.get("seller_birth_date", ""),
            "{{ПРОДАВЕЦ_АДРЕС}}":         data.get("seller_address", ""),
            "{{ПРОДАВЕЦ_ИНИЦИАЛЫ}}":      data.get("seller_initials", ""),
            "{{ПРОДАВЕЦ_ПОЛНЫЕ_ДАННЫЕ}}": data.get("seller_full_details", data.get("seller_name", "")),

            # Идентификационная карта продавца (КР)
            "{{ПРОДАВЕЦ_ID}}":        data.get("seller_id_number", data.get("seller_id", "")),
            "{{ПРОДАВЕЦ_ID_НОМЕР}}":  data.get("seller_id_number", data.get("seller_id", "")),
            "{{ПРОДАВЕЦ_ID_ВЫДАНА}}": data.get("seller_id_issued_by", ""),
            "{{ПРОДАВЕЦ_ID_ДАТА}}":   data.get("seller_id_issued_date", ""),

            # Авто
            "{{МАРКА_МОДЕЛЬ}}": data.get("car_model", ""),
            "{{VIN}}":          data.get("car_vin", ""),
            "{{ГОД_ВЫП}}":      data.get("car_year", ""),
            "{{ЦВЕТ}}":         data.get("car_color", ""),
            "{{НОМ_КУЗОВА}}":   data.get("car_body_number", data.get("car_vin", "")),
            "{{НОМ_ТПО}}":      data.get("tpo_number", ""),
            "{{ДЕНЬ_ТПО}}":     data.get("tpo_day", ""),
            "{{МЕС_ТПО}}":      data.get("tpo_month", ""),
            "{{ГОД_ТПО}}":      data.get("tpo_year", ""),

            # Цена и оплата
            "{{ЦЕНА_ЦИФРАМИ}}":            price_fmt,
            "{{ЦЕНА_ПРОПИСЬЮ}}":           data.get("car_price_words", ""),
            "{{ВАЛЮТА}}":                  data.get("currency", "рублей"),
            "{{СУММА_НАЛИЧНЫМИ}}":         cash_fmt,
            "{{СУММА_НАЛИЧНЫМИ_ПРОПИСЬЮ}}": data.get("cash_amount_words", ""),
            "{{СУММА_ПРОПИСЬЮ}}":           data.get("cash_amount_words", ""),
            "{{ВАЛЮТА_НАЛИЧНЫМИ}}":        data.get("cash_currency", data.get("currency", "рублей")),

            # Банковские реквизиты
            "{{БАНК_КОРР_СТРОКА1}}": data.get("bank_corr_line1", ""),
            "{{БАНК_КОРР_СТРОКА2}}": data.get("bank_corr_line2", ""),
            "{{БАНК_КОРР_СТРОКА3}}": data.get("bank_corr_line3", ""),
            "{{БАНК_ПОЛ_СТРОКА1}}":  data.get("bank_ben_line1", ""),
            "{{БАНК_ПОЛ_СТРОКА2}}":  data.get("bank_ben_line2", ""),
            "{{СЧЕТ_ВАЛЮТА}}":       data.get("account_currency", ""),
            "{{СЧЕТ_НОМЕР}}":        data.get("account_number", ""),
        }

        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

        def replace_in_para(para):
            p_elem = para._element
            runs   = p_elem.findall(f"{{{W}}}r")
            if not runs:
                return

            full_text = "".join(
                t.text or ""
                for r in runs
                for t in r.findall(f"{{{W}}}t")
            )
            if not any(ph in full_text for ph in replacements):
                return

            first_rpr  = runs[0].find(f"{{{W}}}rPr")
            children   = list(p_elem)
            insert_idx = children.index(runs[0])

            new_text = full_text
            for ph, val in replacements.items():
                new_text = new_text.replace(ph, str(val) if val is not None else "")

            for r in runs:
                p_elem.remove(r)

            new_run = etree.Element(f"{{{W}}}r")
            if first_rpr is not None:
                new_run.append(deepcopy(first_rpr))
            new_t = etree.SubElement(new_run, f"{{{W}}}t")
            new_t.text = new_text
            if new_text and (new_text[0] == " " or new_text[-1] == " "):
                new_t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

            p_elem.insert(insert_idx, new_run)

        for para in doc.paragraphs:
            replace_in_para(para)

        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        replace_in_para(para)

        for section in doc.sections:
            for para in section.header.paragraphs:
                replace_in_para(para)
            for para in section.footer.paragraphs:
                replace_in_para(para)

        # ── Проверка на незамещённые плейсхолдеры ──────────────────────
        import re
        leftover = set()

        def scan_para(para):
            text = "".join(t.text or "" for r in para._element.findall(f"{{{W}}}r")
                            for t in r.findall(f"{{{W}}}t"))
            for m in re.findall(r"\{\{[^{}]+\}\}", text):
                leftover.add(m)

        for para in doc.paragraphs:
            scan_para(para)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        scan_para(para)
        for section in doc.sections:
            for para in section.header.paragraphs:
                scan_para(para)
            for para in section.footer.paragraphs:
                scan_para(para)

        if leftover:
            logger.warning(
                f"В документе {output_name}.docx остались незамещённые плейсхолдеры: "
                f"{sorted(leftover)}"
            )

        path = self.output_dir / f"{output_name}.docx"
        doc.save(str(path))
        return str(path)
