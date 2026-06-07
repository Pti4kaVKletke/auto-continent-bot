import os
import base64
import json
import re
import tempfile
from pathlib import Path
from datetime import datetime
import anthropic
import memory
from drive_service import GoogleDriveService
from doc_builder import DocumentBuilder


class DocumentAgent:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.drive = GoogleDriveService()
        self.builder = DocumentBuilder()
        self.model = "claude-opus-4-5"
        memory.init_db()

    def _build_system_prompt(self) -> str:
        """Системный промпт с инструкциями и реквизитами из памяти"""
        base = """Тебя зовут Александра. Ты автоматизированный агент директора компании ОсОО «Авто Континент» (г. Бишкек, Кыргызстан).
Компания занимается продажей автомобилей из Китая, выступает платёжным агентом между покупателями из России и местными продавцами.

У тебя есть РЕАЛЬНЫЕ ИНСТРУМЕНТЫ которые ты ОБЯЗАН использовать:
- create_contract — создать договор купли-продажи и сохранить на Google Drive
- create_invoice — создать счёт на оплату и сохранить на Google Drive
- save_company — сохранить реквизиты компании/клиента в постоянную память
- save_instruction — сохранить инструкцию для себя

ВАЖНО: Когда пользователь просит создать договор или счёт — ВСЕГДА вызывай соответствующий инструмент. Не говори что не можешь — у тебя есть все возможности. Документы автоматически сохраняются на Google Drive.

Отвечай на русском языке. Будь краток и по делу."""

        # Добавляем активные инструкции
        instructions = memory.get_instructions()
        if instructions:
            base += "\n\nТВОИ ПОСТОЯННЫЕ ИНСТРУКЦИИ (всегда выполняй):\n"
            for i in instructions:
                base += f"- {i['text']}\n"

        # Добавляем сохранённые реквизиты
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
        """Основной метод — обрабатываем любое сообщение"""

        # Сохраняем в историю
        memory.add_to_history("user", user_text if not filepath else f"[файл: {filename}] {user_text}")

        # Формируем сообщения для Claude
        history = memory.get_history(limit=15)
        messages = []

        for h in history[:-1]:  # всё кроме последнего (текущего)
            messages.append({"role": h["role"], "content": h["content"]})

        # Текущее сообщение — возможно с файлом
        if filepath:
            current_content = await self._build_file_message(filepath, filename, user_text)
        else:
            current_content = user_text

        messages.append({"role": "user", "content": current_content})

        # Вызываем Claude с инструментами
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=self._build_system_prompt(),
            tools=self._get_tools(),
            messages=messages
        )

        # Обрабатываем ответ
        result = await self._handle_response(response)

        # Сохраняем ответ в историю
        memory.add_to_history("assistant", result.get("text", ""))

        return result

    def _get_tools(self) -> list:
        return [
            {
                "name": "create_contract",
                "description": "Создать договор купли-продажи автомобиля",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "object", "description": "Данные для заполнения договора"},
                        "contract_number": {"type": "string", "description": "Номер договора (опционально)"}
                    },
                    "required": ["data"]
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
                        "name": {"type": "string", "description": "Название компании"},
                        "data": {"type": "object", "description": "Реквизиты компании"}
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
                        "text": {"type": "string", "description": "Текст инструкции"}
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
                    result["files"].append(tool_result)
                    result["text"] += f"\n✅ {tool_result.get('message', '')}"
                elif tool_result.get("message"):
                    result["text"] += f"\n✅ {tool_result['message']}"

        return result

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "create_contract":
            number = tool_input.get("contract_number") or f"АК-{datetime.now().strftime('%d%m%y-%H%M')}"
            date = datetime.now().strftime("%d.%m.%Y")
            filepath = await self.builder.build_contract(tool_input["data"], number, date)
            filename = f"Договор_{number}.docx"
            drive_link = await self.drive.upload_file(filepath, filename, folder="Договоры")
            return {"file": filepath, "filename": filename, "drive_link": drive_link,
                    "message": f"Договор {number} создан и сохранён на Drive"}

        elif tool_name == "create_invoice":
            number = tool_input.get("invoice_number") or f"СЧ-{datetime.now().strftime('%d%m%y-%H%M')}"
            date = datetime.now().strftime("%d.%m.%Y")
            filepath = await self.builder.build_invoice(tool_input["data"], number, date)
            filename = f"Счёт_{number}.docx"
            drive_link = await self.drive.upload_file(filepath, filename, folder="Счета")
            return {"file": filepath, "filename": filename, "drive_link": drive_link,
                    "message": f"Счёт {number} создан и сохранён на Drive"}

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
