import os
import json
import logging
import asyncio
from datetime import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SPREADSHEET_ID = os.environ.get("GOOGLE_SHEETS_ID", "1OHkExAxQzm_3kiOE-h4aGug-MO3yf4OODB8C_fACz08")

# Строка 1 — группы, строка 2 — названия, данные с строки 3
DATA_START_ROW = 3

# Порядок колонок — должен совпадать с format_sheets.py
# (ключ для data dict, или специальное имя)
COLUMNS = [
    "Номер договора",
    "Дата договора",
    "Статус",
    "buyer_name",
    "passport_series",
    "passport_number",
    "buyer_birth_date",
    "buyer_address",
    "buyer_initials",
    "passport_issued_by",
    "passport_issued_date",
    "passport_code",
    "seller_name",
    "seller_id_number",
    "seller_birth_date",
    "seller_address",
    "seller_initials",
    "seller_id_issued_by",
    "seller_id_issued_date",
    "car_model",
    "car_vin",
    "car_year",
    "car_color",
    "tpo_number",
    "car_body_number",
    "tpo_day",
    "tpo_month",
    "tpo_year",
    "car_price",
    "cash_amount",
    "exchange_rate",
    "Комиссия %",
    "car_price_words",
    "currency",
    "cash_amount_words",
    "cash_currency",
    "account_currency",
    "account_number",
    "bank_corr_line1",
    "bank_corr_line2",
    "bank_corr_line3",
    "bank_ben_line1",
    "bank_ben_line2",
    "Папка Drive",
    "Комментарий",
]


def _build_sheets_service():
    oauth_token = os.environ.get("GOOGLE_OAUTH_TOKEN")
    if not oauth_token:
        raise ValueError("GOOGLE_OAUTH_TOKEN не задан")
    token_data = json.loads(oauth_token)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            logger.warning(f"Не удалось обновить токен для Sheets: {e}")
    return build("sheets", "v4", credentials=creds)


class GoogleSheetsService:

    def __init__(self):
        self._service = None

    def _get_service(self):
        if self._service is None:
            self._service = _build_sheets_service()
        return self._service

    def _col_letter(self, idx: int) -> str:
        result = ""
        idx += 1
        while idx:
            idx, rem = divmod(idx - 1, 26)
            result = chr(65 + rem) + result
        return result

    async def save_deal(self, contract_number: str, contract_date: str,
                        data: dict, commission_pct: float,
                        drive_folder_link: str = "") -> bool:
        """Добавляет строку сделки начиная с DATA_START_ROW."""
        def _do():
            try:
                svc = self._get_service()
                sheet = svc.spreadsheets()

                row = []
                for col in COLUMNS:
                    if col == "Номер договора":
                        row.append(contract_number)
                    elif col == "Дата договора":
                        row.append(contract_date)
                    elif col == "Статус":
                        row.append("активна")
                    elif col == "Комиссия %":
                        row.append(str(commission_pct))
                    elif col == "Папка Drive":
                        row.append(drive_folder_link)
                    elif col == "Комментарий":
                        row.append("")
                    else:
                        row.append(str(data.get(col, "")))

                # Добавляем начиная с DATA_START_ROW — строки 1-2 заголовки
                sheet.values().append(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"A{DATA_START_ROW}",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [row]},
                ).execute()
                logger.info(f"Сделка {contract_number} записана в Sheets")
                return True
            except Exception as e:
                logger.error(f"Ошибка записи в Sheets: {e}", exc_info=True)
                return False

        return await asyncio.to_thread(_do)

    async def find_deal(self, query: str) -> list[dict]:
        """Ищет сделки по номеру, ФИО, VIN или дате."""
        def _do():
            try:
                svc = self._get_service()
                sheet = svc.spreadsheets()
                result = sheet.values().get(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"A{DATA_START_ROW}:AZ",
                ).execute()
                rows = result.get("values", [])
                if not rows:
                    return []

                q = query.strip().lower()
                found = []

                for i, row in enumerate(rows, start=DATA_START_ROW):
                    padded = row + [""] * (len(COLUMNS) - len(row))
                    row_dict = dict(zip(COLUMNS, padded))
                    row_dict["__row_index__"] = i

                    searchable = " ".join([
                        row_dict.get("Номер договора", ""),
                        row_dict.get("buyer_name", ""),
                        row_dict.get("seller_name", ""),
                        row_dict.get("car_vin", ""),
                        row_dict.get("Дата договора", ""),
                        row_dict.get("car_model", ""),
                        row_dict.get("Статус", ""),
                    ]).lower()

                    if not q or q in searchable:
                        found.append(row_dict)

                return found
            except Exception as e:
                logger.error(f"Ошибка поиска в Sheets: {e}", exc_info=True)
                return []

        return await asyncio.to_thread(_do)

    async def update_deal(self, contract_number: str, updates: dict) -> bool:
        """Обновляет поля существующей сделки по номеру договора."""
        def _do():
            try:
                svc = self._get_service()
                sheet = svc.spreadsheets()
                result = sheet.values().get(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"A{DATA_START_ROW}:AZ",
                ).execute()
                rows = result.get("values", [])
                if not rows:
                    return False

                target_row = None
                for i, row in enumerate(rows, start=DATA_START_ROW):
                    padded = row + [""] * (len(COLUMNS) - len(row))
                    if padded[0] == contract_number:
                        target_row = i
                        current_row = padded
                        break

                if target_row is None:
                    logger.warning(f"Сделка {contract_number} не найдена в Sheets")
                    return False

                for col_name, new_val in updates.items():
                    if col_name in COLUMNS:
                        idx = COLUMNS.index(col_name)
                        while len(current_row) <= idx:
                            current_row.append("")
                        current_row[idx] = str(new_val)

                last_col = self._col_letter(len(COLUMNS) - 1)
                sheet.values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"A{target_row}:{last_col}{target_row}",
                    valueInputOption="RAW",
                    body={"values": [current_row]},
                ).execute()
                logger.info(f"Сделка {contract_number} обновлена в Sheets")
                return True
            except Exception as e:
                logger.error(f"Ошибка обновления Sheets: {e}", exc_info=True)
                return False

        return await asyncio.to_thread(_do)

    async def cancel_deal(self, contract_number: str, reason: str = "") -> bool:
        """Помечает сделку как отменённую."""
        updates = {"Статус": "отменена"}
        if reason:
            updates["Комментарий"] = reason
        return await self.update_deal(contract_number, updates)

    async def get_deal(self, contract_number: str) -> dict | None:
        """Возвращает данные одной сделки по номеру договора."""
        results = await self.find_deal(contract_number)
        for r in results:
            if r.get("Номер договора") == contract_number:
                return r
        return None
