import os
import base64
import json
import re
import tempfile
from pathlib import Path
from datetime import datetime
import logging
import anthropic
logger = logging.getLogger(__name__)
import memory
from drive_service import GoogleDriveService
from doc_builder import DocumentBuilder


class DocumentAgent:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.drive = GoogleDriveService()
        self.builder = DocumentBuilder()
        self.model = "claude-haiku-4-5-20251001"
        memory.init_db()

    def _build_system_prompt(self) -> str:
        base = """Ты автоматизированный агент директора компании ОсОО «Авто Континент» (г. Бишкек, Кыргызстан).
Компания занимается продажей автомобилей из Китая, выступает платёжным агентом между покупателями из России и местными продавцами.

У тебя есть РЕАЛЬНЫЕ ИНСТРУМЕНТЫ которые ты ОБЯЗАН использовать:
- create_contract — создать договор купли-продажи и сохранить на Google Drive
- create_invoice — создать счёт на оплату и сохранить на Google Drive
- save_company — сохранить реквизиты компании/клиента в постоянную память
- save_instruction — сохранить инструкцию для себя

ВАЖНО: Когда пользователь просит создать договор или счёт — ВСЕГДА вызывай соответствующий инструмент.

=== ОБЯЗАТЕЛЬНЫЕ КЛЮЧИ В ПОЛЕ data ===
При вызове create_contract ты ОБЯЗАН передать data со СТРОГО ЭТИМИ ключами (используй только эти, никакие другие):

ПОКУПАТЕЛЬ (гражданин РФ):
  buyer_name              — ФИО полностью
  buyer_birth_date        — дата рождения (ДД.ММ.ГГГГ)
  buyer_address           — адрес регистрации
  buyer_initials          — инициалы (Иванов И.И.)
  passport_series         — серия паспорта
  passport_number         — номер паспорта
  passport_issued_by      — кем выдан
  passport_issued_date    — дата выдачи (ДД.ММ.ГГГГ)
  passport_code           — код подразделения

ПРОДАВЕЦ (гражданин КР):
  seller_name             — ФИО полностью
  seller_birth_date       — дата рождения (ДД.ММ.ГГГГ)
  seller_address          — адрес регистрации
  seller_initials         — инициалы (Иванов И.И.)
  seller_id_number        — номер идентификационной карты
  seller_id_issued_by     — кем выдана карта
  seller_id_issued_date   — дата выдачи карты (ДД.ММ.ГГГГ)

АВТОМОБИЛЬ:
  car_model               — марка и модель (Toyota RAV4)
  car_vin                 — VIN номер
  car_year                — год выпуска
  car_color               — цвет
  car_body_number         — номер кузова (если есть, иначе VIN)
  tpo_number              — номер ТПО
  tpo_day                 — день выдачи ТПО
  tpo_month               — месяц выдачи ТПО (прописью: января, февраля...)
  tpo_year                — год выдачи ТПО

ФИНАНСЫ — ВАЖНО: это ДВЕ РАЗНЫЕ СУММЫ:
  car_price               — цена автомобиля в ДКП цифрами (например: 4200000). Валюта — рубли.
  car_price_words         — цена ДКП прописью (Четыре миллиона двести тысяч рублей)
  currency                — валюта ДКП (рублей)
  cash_amount             — сумма наличных в Поручении цифрами (например: 54900). Это ДРУГАЯ сумма — в долларах!
  cash_amount_words       — сумма наличных прописью (Пятьдесят четыре тысячи девятьсот долларов)
  cash_currency           — валюта наличных (долларов / сом)
  account_currency        — валюта счёта для банковского перевода
  account_number          — номер счёта
  bank_corr_line1         — реквизиты банка-корреспондента строка 1
  bank_corr_line2         — реквизиты банка-корреспондента строка 2
  bank_corr_line3         — реквизиты банка-корреспондента строка 3
  bank_ben_line1          — реквизиты банка получателя строка 1
  bank_ben_line2          — реквизиты банка получателя строка 2

=== ОБЯЗАТЕЛЬНАЯ ПРОВЕРКА ПЕРЕД ВЫЗОВОМ create_contract ===

Перед тем как вызвать create_contract, ты ОБЯЗАН убедиться что у тебя есть ВСЕ поля из этого списка.
Если хотя бы одно обязательное поле пустое — НЕ создавай договор, а спроси все недостающие данные ОДНИМ сообщением.

ОБЯЗАТЕЛЬНЫЕ поля (без них договор создавать НЕЛЬЗЯ):
  Покупатель: buyer_name, buyer_birth_date, buyer_address, passport_series, passport_number, passport_issued_by, passport_issued_date, passport_code
  Продавец: seller_name, seller_birth_date, seller_address, seller_id_number, seller_id_issued_by
  Автомобиль: car_model, car_vin, car_year, car_color
  Финансы: car_price, car_price_words, currency, cash_amount, cash_amount_words, cash_currency, commission_pct

НЕОБЯЗАТЕЛЬНЫЕ поля (можно оставить пустыми):
  car_body_number, tpo_number, tpo_day, tpo_month, tpo_year
  buyer_initials, seller_initials, seller_id_issued_date
  account_currency, account_number, bank_corr_line1, bank_corr_line2, bank_corr_line3, bank_ben_line1, bank_ben_line2

ПРАВИЛА:
1. car_price и cash_amount — РАЗНЫЕ суммы:
   - car_price = цена в ДКП в РУБЛЯХ (например 4200000). Идёт только в ДКП.
   - cash_amount = сумма наличных которую агент передаёт продавцу в ДОЛЛАРАХ (например 54900). Идёт только в Поручение.
   - НИКОГДА не ставь car_price в поле cash_amount. Это всегда разные числа.
   - cash_amount_words — сумма прописью БЕЗ названия валюты. Только число словами: "Пятьдесят четыре тысячи девятьсот". Валюта указывается отдельно в поле cash_currency.
2. Если пользователь не назвал цвет автомобиля — обязательно спроси.
3. Если пользователь не назвал сумму наличных (cash_amount) отдельно — спроси.
4. Собери все недостающие поля в ОДНОМ вопросе, не задавай по одному.
5. Когда все обязательные поля собраны — ПЕРЕД вызовом create_contract выведи сводку всех данных в чат в таком формате и жди подтверждения:

📋 ПРОВЕРЬТЕ ДАННЫЕ ПЕРЕД СОЗДАНИЕМ ДОГОВОРА:

👤 ПОКУПАТЕЛЬ:
  ФИО: ...
  Дата рождения: ...
  Адрес: ...
  Паспорт: серия ... № ..., выдан: ..., дата: ..., код: ...

👤 ПРОДАВЕЦ:
  ФИО: ...
  Дата рождения: ...
  Адрес: ...
  Идентификационная карта № ..., выдана: ...

🚗 АВТОМОБИЛЬ:
  Марка/модель: ...
  VIN: ...
  Год: ...
  Цвет: ...

💰 ФИНАНСЫ:
  Цена в ДКП (рублей): ...
  Цена прописью: ...
  Сумма наличными агенту (долларов): ...
  Сумма наличными прописью (без валюты): ...
  Комиссия: ...%

Всё верно? Создаю договоры?

После этого жди ответа пользователя. Только если он подтвердил (написал "да", "верно", "создавай", "всё верно" или аналог) — вызывай create_contract.

Если каких-то необязательных данных нет — оставь значение пустой строкой "".
Отвечай на русском языке. Будь краток и по делу."""

        instructions = memory.get_instructions()
        if instructions:
            base += "\n\nТВОИ ПОСТОЯННЫЕ ИНСТРУКЦИИ (всегда выполняй):\n"
            for i in instructions:
                base += f"- {i['text']}\n"

        companies = memory.list_companies()
        if companies:
            base += "\n\nСОХРАНЁННЫЕ РЕКВИЗИТЫ КОМПАНИЙ:\n"
            for c in companies:
                data = memory.get_company(c["name"])
                base += f"\n{c['name']}:\n"
                for k, v in data.items():
                    if v:
                        base += f"  {k}: {v}\n"

        return base

    async def process_message(self, user_text: str, filepath: str = None, filename: str = None) -> dict:
        memory.add_to_history("user", user_text if not filepath else f"[файл: {filename}] {user_text}")

        history = memory.get_history(limit=15)
        messages = []
        for h in history[:-1]:
            messages.append({"role": h["role"], "content": h["content"]})

        if filepath:
            current_content = await self._build_file_message(filepath, filename, user_text)
        else:
            current_content = user_text

        messages.append({"role": "user", "content": current_content})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=self._build_system_prompt(),
            tools=self._get_tools(),
            messages=messages
        )

        result = await self._handle_response(response)
        memory.add_to_history("assistant", result.get("text", ""))
        return result

    def _get_tools(self) -> list:
        return [
            {
                "name": "create_contract",
                "description": "Создать полный пакет документов по сделке (АГ договор, ДКП ТС, Счёт на оплату). ВСЕГДА спрашивай размер комиссии в процентах перед вызовом если он не указан.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "data": {
                            "type": "object",
                            "description": "Данные для заполнения документов — строго по ключам из системного промпта"
                        },
                        "contract_number": {"type": "string", "description": "Номер договора (опционально)"},
                        "commission_pct": {"type": "number", "description": "Комиссия агента в процентах, например 2.0"}
                    },
                    "required": ["data", "commission_pct"]
                }
            },
            {
                "name": "create_invoice",
                "description": "Создать счёт на оплату",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "object", "description": "Данные для заполнения счёта"},
                        "invoice_number": {"type": "string", "description": "Номер счёта (опционально)"}
                    },
                    "required": ["data"]
                }
            },
            {
                "name": "save_company",
                "description": "Сохранить реквизиты компании или клиента в память",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "data": {"type": "object"}
                    },
                    "required": ["name", "data"]
                }
            },
            {
                "name": "save_instruction",
                "description": "Сохранить постоянную инструкцию для агента",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"}
                    },
                    "required": ["text"]
                }
            }
        ]

    async def _handle_response(self, response) -> dict:
        result = {"text": "", "files": [], "success": True}

        for block in response.content:
            if block.type == "text":
                result["text"] += block.text

            elif block.type == "tool_use":
                tool_result = await self._execute_tool(block.name, block.input)

                if tool_result.get("file"):
                    result["files"].append({
                        "file": tool_result["file"],
                        "filename": tool_result["filename"],
                        "drive_link": tool_result.get("drive_link", "")
                    })

                # Дополнительные файлы (ДКП, Счёт, PDF)
                extra_files = tool_result.get("extra_files", [])
                extra_names = tool_result.get("extra_names", [])
                for f_path, f_name in zip(extra_files, extra_names):
                    if Path(f_path).exists():
                        result["files"].append({
                            "file": f_path,
                            "filename": f_name,
                            "drive_link": ""
                        })

                if tool_result.get("message"):
                    result["text"] += f"\n✅ {tool_result['message']}"

        return result

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "create_contract":
            number = tool_input.get("contract_number") or datetime.now().strftime("%d%m%y") + "001"
            import json as _json
            logger.info("=== DATA KEYS: " + str(list(tool_input.get("data", {}).keys())))
            logger.info("=== BANK FIELDS: corr1=" + repr(tool_input.get("data", {}).get("bank_corr_line1")) + " pol1=" + repr(tool_input.get("data", {}).get("bank_ben_line1")))
            date = datetime.now().strftime("%d.%m.%Y")
            commission_pct = float(tool_input.get("commission_pct", 1.0))

            deal_folder_id = await self.drive.get_or_create_deal_folder(number)

            # 1. Агентский договор
            ag_path = await self.builder.build_contract(tool_input["data"], number, date, commission_pct)
            ag_docx = f"АГ_Договор_{number}.docx"
            ag_pdf  = f"АГ_Договор_{number}.pdf"
            ag_pdf_path = await self.builder.convert_to_pdf(ag_path)
            await self.drive.upload_file(ag_path, ag_docx, deal_folder_id)
            ag_link = await self.drive.upload_file(ag_pdf_path, ag_pdf, deal_folder_id)

            # 2. ДКП ТС
            dkp_path = await self.builder.build_dkp(tool_input["data"], number, date)
            dkp_docx = f"ДКП_ТС_{number}.docx"
            dkp_pdf  = f"ДКП_ТС_{number}.pdf"
            dkp_pdf_path = await self.builder.convert_to_pdf(dkp_path)
            await self.drive.upload_file(dkp_path, dkp_docx, deal_folder_id)
            await self.drive.upload_file(dkp_pdf_path, dkp_pdf, deal_folder_id)

            # 3. Счёт
            invoice_path = await self.builder.build_invoice(tool_input["data"], number, date, commission_pct)
            inv_xlsx = f"Счёт_{number}.xlsx"
            inv_pdf  = f"Счёт_{number}.pdf"
            inv_pdf_path = await self.builder.convert_to_pdf(invoice_path)
            await self.drive.upload_file(invoice_path, inv_xlsx, deal_folder_id)
            await self.drive.upload_file(inv_pdf_path, inv_pdf, deal_folder_id)

            # Собираем список файлов для отправки в Telegram (только существующие)
            extra_files = []
            extra_names = []
            for fpath, fname in [
                (ag_pdf_path,   ag_pdf),
                (dkp_path,      dkp_docx),
                (dkp_pdf_path,  dkp_pdf),
                (invoice_path,  inv_xlsx),
                (inv_pdf_path,  inv_pdf),
            ]:
                if Path(fpath).exists() and Path(fpath).stat().st_size > 0:
                    extra_files.append(fpath)
                    extra_names.append(fname)

            uploaded = 1 + len(extra_files)  # АГ docx + остальные
            return {
                "file": ag_path,
                "filename": ag_docx,
                "extra_files": extra_files,
                "extra_names": extra_names,
                "drive_link": ag_link,
                "message": f"Сделка {number} создана — {uploaded} файлов сохранено на Drive"
            }

        elif tool_name == "create_invoice":
            number = tool_input.get("invoice_number") or datetime.now().strftime("%d%m%y") + "001"
            date = datetime.now().strftime("%d.%m.%Y")
            deal_folder_id = await self.drive.get_or_create_deal_folder(number)
            invoice_path = await self.builder.build_invoice(tool_input["data"], number, date)
            inv_pdf_path = await self.builder.convert_to_pdf(invoice_path)
            await self.drive.upload_file(invoice_path, f"Счёт_{number}.xlsx", deal_folder_id)
            link = await self.drive.upload_file(inv_pdf_path, f"Счёт_{number}.pdf", deal_folder_id)
            return {
                "file": invoice_path,
                "filename": f"Счёт_{number}.xlsx",
                "drive_link": link,
                "message": f"Счёт {number} создан и сохранён на Drive"
            }

        elif tool_name == "save_company":
            memory.save_company(tool_input["name"], tool_input["data"])
            return {"message": f"Реквизиты «{tool_input['name']}» сохранены"}

        elif tool_name == "save_instruction":
            memory.add_instruction(tool_input["text"])
            return {"message": f"Инструкция сохранена: {tool_input['text']}"}

        return {"message": "Выполнено"}

    async def _build_file_message(self, filepath: str, filename: str, user_text: str) -> list:
        ext = Path(filename).suffix.lower()
        content = []

        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
            with open(filepath, "rb") as f:
                data = base64.standard_b64encode(f.read()).decode("utf-8")
            media_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_map.get(ext.strip("."), "image/jpeg"), "data": data}
            })
        elif ext == ".pdf":
            with open(filepath, "rb") as f:
                data = base64.standard_b64encode(f.read()).decode("utf-8")
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data}
            })
        else:
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()[:5000]
                content.append({"type": "text", "text": f"Содержимое файла {filename}:\n{text}"})
            except Exception:
                pass

        prompt = user_text or "Извлеки все данные из этого документа: реквизиты, данные об автомобиле, суммы."
        content.append({"type": "text", "text": prompt})
        return content

    async def process_file(self, filepath: str, filename: str) -> dict:
        return await self.process_message(
            "Извлеки все данные из документа и скажи что нашёл.",
            filepath=filepath,
            filename=filename
        )
