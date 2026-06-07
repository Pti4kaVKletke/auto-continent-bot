import os
import tempfile
from pathlib import Path
from datetime import datetime
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT


class DocumentBuilder:
    def __init__(self):
        # Папка с вашими шаблонами (если есть)
        self.templates_dir = Path(os.environ.get("TEMPLATES_DIR", "./templates"))
        self.output_dir = Path(tempfile.gettempdir()) / "tg_agent_docs"
        self.output_dir.mkdir(exist_ok=True)

    async def build_contract(self, data: dict, contract_number: str, date: str) -> str:
        """Создаём договор. Если есть шаблон — заполняем его, иначе генерируем."""
        template_path = self.templates_dir / "contract_template.docx"

        if template_path.exists():
            return await self._fill_template(template_path, data, contract_number, date, "contract")
        else:
            return await self._generate_contract(data, contract_number, date)

    async def build_invoice(self, data: dict, invoice_number: str, date: str) -> str:
        """Создаём счёт."""
        template_path = self.templates_dir / "invoice_template.docx"

        if template_path.exists():
            return await self._fill_template(template_path, data, invoice_number, date, "invoice")
        else:
            return await self._generate_invoice(data, invoice_number, date)

    async def _fill_template(self, template_path: Path, data: dict,
                              number: str, date: str, doc_type: str) -> str:
        """Заполняем шаблон .docx — заменяем плейсхолдеры на данные"""
        doc = Document(str(template_path))

        # Словарь замен — добавьте свои плейсхолдеры из шаблона
        replacements = {
            "{{НОМЕР}}": number,
            "{{ДАТА}}": date,
            "{{КОМПАНИЯ}}": data.get("company_name", ""),
            "{{ИНН}}": data.get("inn", ""),
            "{{КПП}}": data.get("kpp", ""),
            "{{АДРЕС}}": data.get("address", ""),
            "{{ТЕЛЕФОН}}": data.get("phone", ""),
            "{{БАНК}}": data.get("bank_name", ""),
            "{{РАСЧ_СЧЕТ}}": data.get("bank_account", ""),
            "{{КОРР_СЧЕТ}}": data.get("correspondent_account", ""),
            "{{БИК}}": data.get("bik", ""),
            "{{МОДЕЛЬ_АВТО}}": data.get("car_model", ""),
            "{{VIN}}": data.get("car_vin", ""),
            "{{ГОД}}": data.get("car_year", ""),
            "{{ЦВЕТ}}": data.get("car_color", ""),
            "{{ЦЕНА}}": data.get("car_price", ""),
            "{{ВАЛЮТА}}": data.get("currency", "RUB"),
            "{{КОНТАКТ}}": data.get("contact_person", ""),
        }

        # Заменяем во всех параграфах
        for paragraph in doc.paragraphs:
            for placeholder, value in replacements.items():
                if placeholder in paragraph.text:
                    for run in paragraph.runs:
                        if placeholder in run.text:
                            run.text = run.text.replace(placeholder, value)

        # Заменяем в таблицах
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        for placeholder, value in replacements.items():
                            if placeholder in paragraph.text:
                                for run in paragraph.runs:
                                    if placeholder in run.text:
                                        run.text = run.text.replace(placeholder, value)

        output_path = self.output_dir / f"{doc_type}_{number}.docx"
        doc.save(str(output_path))
        return str(output_path)

    async def _generate_contract(self, data: dict, contract_number: str, date: str) -> str:
        """Генерируем договор с нуля (если нет шаблона)"""
        doc = Document()

        # Настройка страницы
        section = doc.sections[0]
        section.page_width = Cm(21)
        section.page_height = Cm(29.7)
        section.left_margin = Cm(3)
        section.right_margin = Cm(1.5)
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)

        # Заголовок
        title = doc.add_paragraph()
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = title.add_run(f"ДОГОВОР КУПЛИ-ПРОДАЖИ АВТОМОБИЛЯ")
        run.bold = True
        run.font.size = Pt(14)

        subtitle = doc.add_paragraph()
        subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
        subtitle.add_run(f"№ {contract_number} от {date} г.")

        doc.add_paragraph()

        # Стороны
        buyer = data.get("company_name", "____________")
        inn = data.get("inn", "")
        address = data.get("address", "")

        intro = doc.add_paragraph()
        intro.add_run("ООО «Авто Континент»").bold = True
        intro.add_run(f', именуемое в дальнейшем «Продавец», с одной стороны, и ')
        intro.add_run(f'{buyer}').bold = True
        if inn:
            intro.add_run(f', ИНН {inn}')
        intro.add_run(f', именуемое в дальнейшем «Покупатель», с другой стороны, заключили настоящий договор о следующем:')

        doc.add_paragraph()

        # Секции договора
        sections_data = [
            ("1. ПРЕДМЕТ ДОГОВОРА", [
                f"1.1. Продавец обязуется передать в собственность Покупателя, а Покупатель обязуется принять и оплатить следующий автомобиль:",
                f"Марка/модель: {data.get('car_model', '_____________')}",
                f"VIN: {data.get('car_vin', '_____________')}",
                f"Год выпуска: {data.get('car_year', '_____')}",
                f"Цвет: {data.get('car_color', '_____________')}",
            ]),
            ("2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ", [
                f"2.1. Цена автомобиля составляет {data.get('car_price', '_____________')} {data.get('currency', 'RUB')}.",
                "2.2. Оплата производится в течение 3 (трёх) банковских дней с момента подписания договора.",
                "2.3. Датой оплаты считается дата поступления денежных средств на расчётный счёт Продавца.",
            ]),
            ("3. ПЕРЕДАЧА АВТОМОБИЛЯ", [
                "3.1. Продавец обязуется передать автомобиль Покупателю в течение 5 (пяти) рабочих дней после получения оплаты.",
                "3.2. Передача автомобиля оформляется актом приёма-передачи.",
            ]),
            ("4. ОТВЕТСТВЕННОСТЬ СТОРОН", [
                "4.1. За неисполнение или ненадлежащее исполнение обязательств по настоящему договору стороны несут ответственность в соответствии с действующим законодательством.",
            ]),
            ("5. РЕКВИЗИТЫ СТОРОН", [
                "Продавец: ООО «Авто Континент»",
                f"Покупатель: {buyer}" + (f"\nИНН: {inn}" if inn else "") + (f"\nАдрес: {address}" if address else ""),
                f"Банк Покупателя: {data.get('bank_name', '')}",
                f"Р/с: {data.get('bank_account', '')}",
                f"БИК: {data.get('bik', '')}",
            ]),
        ]

        for section_title, items in sections_data:
            h = doc.add_paragraph()
            h.add_run(section_title).bold = True
            for item in items:
                p = doc.add_paragraph(item)
                p.paragraph_format.space_after = Pt(3)

        doc.add_paragraph()

        # Подписи
        sig_table = doc.add_table(rows=2, cols=2)
        sig_table.alignment = WD_TABLE_ALIGNMENT.CENTER

        sig_table.cell(0, 0).text = "Продавец:"
        sig_table.cell(0, 1).text = "Покупатель:"
        sig_table.cell(1, 0).text = "\n\n_________________ /____________/"
        sig_table.cell(1, 1).text = "\n\n_________________ /____________/"

        output_path = self.output_dir / f"contract_{contract_number}.docx"
        doc.save(str(output_path))
        return str(output_path)

    async def _generate_invoice(self, data: dict, invoice_number: str, date: str) -> str:
        """Генерируем счёт с нуля"""
        doc = Document()

        section = doc.sections[0]
        section.page_width = Cm(21)
        section.page_height = Cm(29.7)
        section.left_margin = Cm(2)
        section.right_margin = Cm(2)
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)

        # Заголовок
        title = doc.add_paragraph()
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = title.add_run(f"СЧЁТ НА ОПЛАТУ № {invoice_number}")
        run.bold = True
        run.font.size = Pt(14)

        doc.add_paragraph(f"от {date} г.")
        doc.add_paragraph()

        # Реквизиты
        doc.add_paragraph(f"Поставщик: ООО «Авто Континент»")
        doc.add_paragraph(f"Покупатель: {data.get('company_name', '_____________')}")
        if data.get("inn"):
            doc.add_paragraph(f"ИНН: {data['inn']}")
        if data.get("address"):
            doc.add_paragraph(f"Адрес: {data['address']}")
        doc.add_paragraph()

        # Таблица товаров
        table = doc.add_table(rows=2, cols=5)
        table.style = "Table Grid"

        headers = ["№", "Наименование", "Кол-во", "Цена", "Сумма"]
        for i, h in enumerate(headers):
            cell = table.cell(0, i)
            cell.text = h
            cell.paragraphs[0].runs[0].bold = True

        # Строка с автомобилем
        row = table.rows[1]
        car_name = f"{data.get('car_model', 'Автомобиль')} {data.get('car_year', '')} VIN: {data.get('car_vin', '')}".strip()
        price = data.get("car_price", "0")
        currency = data.get("currency", "RUB")

        row.cells[0].text = "1"
        row.cells[1].text = car_name
        row.cells[2].text = "1 шт."
        row.cells[3].text = f"{price} {currency}"
        row.cells[4].text = f"{price} {currency}"

        doc.add_paragraph()
        total_p = doc.add_paragraph()
        total_p.add_run(f"ИТОГО: {price} {currency}").bold = True
        doc.add_paragraph()

        # Банковские реквизиты
        if any([data.get("bank_name"), data.get("bank_account"), data.get("bik")]):
            doc.add_paragraph("Банковские реквизиты для оплаты:").runs[0].bold = True
            if data.get("bank_name"):
                doc.add_paragraph(f"Банк: {data['bank_name']}")
            if data.get("bank_account"):
                doc.add_paragraph(f"Р/с: {data['bank_account']}")
            if data.get("bik"):
                doc.add_paragraph(f"БИК: {data['bik']}")
            if data.get("correspondent_account"):
                doc.add_paragraph(f"К/с: {data['correspondent_account']}")

        output_path = self.output_dir / f"invoice_{invoice_number}.docx"
        doc.save(str(output_path))
        return str(output_path)
