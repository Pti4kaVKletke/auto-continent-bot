import os
import json
import logging
import asyncio
import urllib.request
from datetime import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SPREADSHEET_ID = os.environ.get("GOOGLE_SHEETS_ID", "1OHkExAxQzm_3kiOE-h4aGug-MO3yf4OODB8C_fACz08")


def _save_token_to_railway(creds: Credentials, original_token_data: dict):
    """Сохраняет обновлённый OAuth токен обратно в Railway (аналогично drive_service.py)."""
    try:
        updated = dict(original_token_data)
        updated["token"] = creds.token
        if creds.expiry:
            updated["token_expiry"] = creds.expiry.isoformat()

        new_value = json.dumps(updated)

        query = """
        mutation UpsertVariables($input: ServiceVariablesInput!) {
          serviceVariablesUpsert(input: $input)
        }
        """
        railway_token = os.environ.get("RAILWAY_API_TOKEN", "")
        railway_service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
        if not railway_token or not railway_service_id:
            logger.warning("RAILWAY_API_TOKEN или RAILWAY_SERVICE_ID не заданы — токен Sheets не сохранён в Railway")
            return

        variables = {
            "input": {
                "serviceId": railway_service_id,
                "environmentId": os.environ.get("RAILWAY_ENVIRONMENT_ID", ""),
                "variables": {"GOOGLE_OAUTH_TOKEN": new_value},
            }
        }
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(
            "https://backboard.railway.app/graphql/v2",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {railway_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if "errors" in result:
                logger.warning(f"Railway API (Sheets): ошибки: {result['errors']}")
            else:
                os.environ["GOOGLE_OAUTH_TOKEN"] = new_value
                logger.info("Sheets OAuth токен сохранён в Railway")
    except Exception as e:
        logger.warning(f"Не удалось сохранить Sheets токен в Railway: {e}")

# Строка 1 — группы, строка 2 — названия, данные с строки 3
DATA_START_ROW = 3

# Порядок колонок — должен совпадать с format_sheets.py
# (ключ для data dict, или специальное имя)
COLUMNS = [
    "Номер договора",
    "Дата договора",
    "Сумма Договора",
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
    "bank_kpp",
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
            logger.info("Sheets OAuth токен обновлён")
            _save_token_to_railway(creds, token_data)
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

    async def _sheets_retry(self, fn, *args, retries=3, **kwargs):
        """Выполняет синхронную функцию в потоке с retry при временных ошибках Sheets."""
        import time
        for attempt in range(retries):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except Exception as e:
                err_str = str(e)
                # Повторяем только при rate limit (429) или сетевых ошибках
                if attempt < retries - 1 and any(x in err_str for x in ["429", "503", "quota", "Connection"]):
                    wait = 2 ** attempt  # 1с, 2с
                    logger.warning(f"Sheets ошибка (попытка {attempt+1}/{retries}), жду {wait}с: {e}")
                    await asyncio.sleep(wait)
                    self._service = None  # Сбрасываем кэш сервиса
                else:
                    raise
        return None

    async def save_deal(self, contract_number: str, contract_date: str,
                        data: dict, commission_pct: float,
                        drive_folder_link: str = "") -> bool:
        """Добавляет строку сделки начиная с DATA_START_ROW."""
        def _do():
            svc = self._get_service()
            sheet = svc.spreadsheets()

            row = []
            # Вычисляем итоговую сумму: цена + комиссия
            try:
                price_val = float(str(data.get("car_price", "0")).replace(" ", "").replace(",", "."))
            except Exception:
                price_val = 0.0
            total_sum = round(price_val * (1 + commission_pct / 100), 2)

            for col in COLUMNS:
                if col == "Номер договора":
                    row.append(contract_number)
                elif col == "Дата договора":
                    row.append(contract_date)
                elif col == "Сумма Договора":
                    row.append(f"{total_sum:.2f}" if total_sum > 0 else "")
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

            sheet.values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"A{DATA_START_ROW}",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
            logger.info(f"Сделка {contract_number} записана в Sheets")
            return True

        try:
            return await self._sheets_retry(_do)
        except Exception as e:
            logger.error(f"Ошибка записи в Sheets: {e}", exc_info=True)
            return False

    async def find_deal(self, query: str) -> list[dict]:
        """Ищет сделки по номеру, ФИО, VIN или дате."""
        def _do():
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

        try:
            return await self._sheets_retry(_do) or []
        except Exception as e:
            logger.error(f"Ошибка поиска в Sheets: {e}", exc_info=True)
            return []

    async def update_deal(self, contract_number: str, updates: dict) -> bool:
        """Обновляет поля существующей сделки по номеру договора."""
        def _do():
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

            # Пересчитываем "Сумма Договора" если менялась цена или комиссия
            if "car_price" in updates or "Комиссия %" in updates:
                try:
                    price_idx = COLUMNS.index("car_price")
                    comm_idx  = COLUMNS.index("Комиссия %")
                    sum_idx   = COLUMNS.index("Сумма Договора")
                    price_val = float(str(current_row[price_idx]).replace(" ", "").replace(",", "."))
                    comm_pct  = float(str(current_row[comm_idx] or "1").replace(",", "."))
                    total_sum = round(price_val * (1 + comm_pct / 100), 2)
                    current_row[sum_idx] = f"{total_sum:.2f}" if total_sum > 0 else ""
                except Exception as e:
                    logger.warning(f"Не удалось пересчитать сумму договора: {e}")

            last_col = self._col_letter(len(COLUMNS) - 1)
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"A{target_row}:{last_col}{target_row}",
                valueInputOption="RAW",
                body={"values": [current_row]},
            ).execute()
            logger.info(f"Сделка {contract_number} обновлена в Sheets")
            return True

        try:
            return await self._sheets_retry(_do)
        except Exception as e:
            logger.error(f"Ошибка обновления Sheets: {e}", exc_info=True)
            return False

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
