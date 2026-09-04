import os
import re
import json
import logging
import asyncio
import urllib.request
from datetime import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import bank_requisites as br
import memory

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
    # ── Колонки, добавленные 19.08.2026 ───────────────────────────────────
    # Порядок здесь обязан совпадать с физическим порядком колонок в таблице:
    # запись и обновление строки идут по индексу. Четыре новые колонки Илья
    # вставил в блок «ОСНОВНОЕ» (C, D, F, G, H) — список приведён к тому же виду.
    "Номер ДКП",     # последние 6 знаков VIN; пусто → считается из VIN
    "Дата ДКП",      # ДКП может быть подписан раньше агентского договора.
                     # Пусто → подставляется «Дата договора».
    "Сумма Договора",
    "Сумма Комиссии",  # = car_price × «Комиссия %» / 100 → {{КОМИССИЯ_ЦИФРАМИ}}
                       # проверка: car_price + Сумма Комиссии = Сумма Договора
    "Дата поступления",  # дата зачисления средств от принципала → {{ДАТА_ПОСТУПЛЕНИЯ}}
                         # пусто → берётся дата последнего платежа из «Платежи»
    "Дата расчёта",      # дата передачи наличных продавцу → {{ДАТА_РАСЧЕТА}};
                         # ею же датируются акт, расписка и отчёт агента
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
    "Фактический курс",  # курс конвертации по факту → {{КУРС_ФАКТИЧЕСКИЙ}};
                         # заполняется вручную после конвертации, перед распиской.
                         # «exchange_rate» выше — курс на дату поручения,
                         # он уходит только в {{КУРС_ПОРУЧЕНИЯ}}.
    "Сумма выдана (USD)",  # фактически выданная сумма → {{СУММА_НАЛИЧНЫМИ}}.
                           # = цена ДКП / «Фактический курс», округлённая до цента.
                           # Бот считает её при первой сборке расписки/акта/отчёта
                           # и СРАЗУ пишет сюда — чтобы повторная генерация не дала
                           # другое число, чем в уже подписанной расписке.
                           # «cash_amount» выше остаётся расчётной и уходит
                           # только в {{СУММА_ПОРУЧЕНИЯ}}.
    "Комиссия %",
    "car_price_words",
    "currency",
    "cash_amount_words",
    "cash_currency",
    "account_currency",
    "account_number",
    # ── Реквизиты счёта, нормализованные 04.09.2026 ───────────────────────
    # Восемь колонок на месте прежних шести (bank_corr_line1..3,
    # bank_ben_line1..2, bank_kpp). Здесь только то, что принадлежит СЧЁТУ:
    # наименование получателя, оба ИНН и КПП живут в карточке компании
    # (company.py) и по сделкам не дублируются.
    "account_type",     # direct_rf | corr — ЯВНЫЙ тип, не догадка по пустоте
    "bank_name",        # банк, где открыт счёт
    "bank_bic",         # его БИК
    "bank_corr_acc",    # его корр. счёт
    "bank_swift",       # SWIFT — необязателен, нужен для карточки контрагенту
    "corr_bank_name",   # банк-корреспондент (только для corr)
    "corr_bank_bic",
    "corr_bank_acc",
    "Комментарий",
    "Платежи",       # текст "500000 (01.07.2026); 300000 (15.07.2026)"
    "Получено",      # сумма всех платежей
    "Остаток",       # Сумма Договора - Получено
    # Блок «Файлы»: ссылка на папку и состояние сканов стоят рядом —
    # обе про одно и то же место на Drive.
    "Папка Drive",
    "Сканы",         # состояние подписанных сканов в папке Drive:
                     # «3/5 · нет: акт, отчёт агента» или «5/5 ✓».
                     # Пересчитывается ботом по именам файлов в папке «Сканы»
                     # при каждой загрузке скана и при открытии их списка.
                     # Источник истины — Drive, колонка только отражает его.

]


def _dkp_number(data: dict) -> str:
    """
    Номер ДКП = последние 6 знаков VIN.

    Дублирует doc_builder.dkp_number_from намеренно: тянуть сюда весь
    doc_builder (python-docx, openpyxl) ради шести символов незачем.
    Логику менять сразу в обоих местах.
    """
    manual = str(data.get("Номер ДКП") or data.get("dkp_number") or "").strip()
    if manual:
        return manual
    vin = re.sub(r"[^A-Za-z0-9]", "", str(data.get("car_vin") or ""))
    return vin[-6:].upper() if len(vin) >= 6 else ""


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
                        deal_data: dict, commission_pct: float,
                        drive_folder_link: str = "") -> bool:
        """Добавляет строку сделки начиная с DATA_START_ROW."""
        def _do():
            svc = self._get_service()
            sheet = svc.spreadsheets()

            # Реквизиты приводим к модели один раз здесь.
            data = {**deal_data, **br.normalize(deal_data)}

            row = []
            # Вычисляем итоговую сумму: цена + комиссия
            try:
                price_val = float(str(data.get("car_price", "0")).replace(" ", "").replace(",", "."))
            except Exception:
                price_val = 0.0
            commission_sum = round(price_val * commission_pct / 100, 2)
            total_sum = round(price_val + commission_sum, 2)

            for col in COLUMNS:
                if col == "Номер договора":
                    row.append(contract_number)
                elif col == "Дата договора":
                    row.append(contract_date)
                elif col == "Дата ДКП":
                    # Отдельная дата ДКП: он может быть подписан раньше
                    # агентского договора. Если не указана — равна дате договора.
                    row.append(str(data.get("dkp_date") or "").strip() or contract_date)
                elif col == "Сумма Договора":
                    row.append(f"{total_sum:.2f}".replace(".", ",") if total_sum > 0 else "")
                elif col == "Статус":
                    row.append("активна")
                elif col == "Комиссия %":
                    row.append(str(commission_pct).replace(".", ","))
                elif col == "Сумма Комиссии":
                    # Сумма агентского вознаграждения. Пишется в журнал, а не
                    # считается каждым документом отдельно: цифра идёт в счёт,
                    # поручение, акт и отчёт и обязана совпадать во всех.
                    row.append(f"{commission_sum:.2f}".replace(".", ",")
                               if commission_sum > 0 else "")
                elif col == "Номер ДКП":
                    # Последние 6 знаков VIN (см. dkp_number_from в doc_builder).
                    row.append(_dkp_number(data))
                elif col == "Папка Drive":
                    row.append(drive_folder_link)
                elif col == "Комментарий":
                    row.append("")
                else:
                    row.append(str(data.get(col, "")))

            sheet.values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"A{DATA_START_ROW}",
                valueInputOption="USER_ENTERED",
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
                range=f"A{DATA_START_ROW}:{self._col_letter(len(COLUMNS) - 1)}",
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
                range=f"A{DATA_START_ROW}:{self._col_letter(len(COLUMNS) - 1)}",
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

            # Реквизиты правим только целиком. Смена одного поля (например
            # профиля через кнопку «Реквизиты») обязана пересчитать и тип счёта,
            # и ИНН, и старую раскладку — иначе в шапке счёта окажется ИНН одной
            # юрисдикции, а банк другой, и ошибка ничем себя не проявит.
            if set(br.ALL_FIELDS) & set(updates):
                row_dict = dict(zip(COLUMNS, current_row))
                payload = br.normalize(row_dict)
                for k, v in payload.items():
                    if k in COLUMNS:
                        idx = COLUMNS.index(k)
                        while len(current_row) <= idx:
                            current_row.append("")
                        current_row[idx] = str(v)

            # Пересчитываем «Сумма Комиссии» и «Сумма Договора», если менялась
            # цена или процент. Обе цифры уходят в счёт, поручение, акт и отчёт —
            # оставить их расходиться со ставкой нельзя.
            if "car_price" in updates or "Комиссия %" in updates:
                try:
                    price_idx = COLUMNS.index("car_price")
                    comm_idx  = COLUMNS.index("Комиссия %")
                    comm_sum_idx = COLUMNS.index("Сумма Комиссии")
                    sum_idx   = COLUMNS.index("Сумма Договора")
                    price_val = float(str(current_row[price_idx]).replace(" ", "").replace(",", "."))
                    comm_pct  = float(str(current_row[comm_idx] or "1").replace(",", "."))
                    comm_sum  = round(price_val * comm_pct / 100, 2)
                    total_sum = round(price_val + comm_sum, 2)
                    current_row[comm_sum_idx] = f"{comm_sum:.2f}".replace(".", ",") if comm_sum > 0 else ""
                    current_row[sum_idx] = f"{total_sum:.2f}".replace(".", ",") if total_sum > 0 else ""
                except Exception as e:
                    logger.warning(f"Не удалось пересчитать сумму договора: {e}")

            # VIN поменялся → номер ДКП тоже (он и есть последние 6 знаков VIN).
            if "car_vin" in updates:
                try:
                    dkp_idx = COLUMNS.index("Номер ДКП")
                    current_row[dkp_idx] = _dkp_number({"car_vin": updates["car_vin"]})
                except Exception as e:
                    logger.warning(f"Не удалось пересчитать номер ДКП: {e}")

            last_col = self._col_letter(len(COLUMNS) - 1)
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"A{target_row}:{last_col}{target_row}",
                valueInputOption="USER_ENTERED",
                body={"values": [current_row]},
            ).execute()
            logger.info(f"Сделка {contract_number} обновлена в Sheets")
            return True

        try:
            return await self._sheets_retry(_do)
        except Exception as e:
            logger.error(f"Ошибка обновления Sheets: {e}", exc_info=True)
            return False

    async def batch_update_column(self, col_name: str, values: dict) -> int:
        """Пишет ОДНУ колонку сразу многим сделкам одним запросом.

        values: {номер договора: значение}. Трогаем только эти ячейки —
        остальные 56 колонок не переписываются, поэтому параллельная правка
        сделки ничего не затирает (в отличие от update_deal, который читает и
        пишет строку целиком). Возвращает число записанных ячеек.
        """
        if col_name not in COLUMNS or not values:
            return 0
        col = self._col_letter(COLUMNS.index(col_name))

        def _do():
            svc = self._get_service()
            sheet = svc.spreadsheets()
            nums = sheet.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"A{DATA_START_ROW}:A",
            ).execute().get("values", [])

            data = []
            for i, row in enumerate(nums, start=DATA_START_ROW):
                num = (row[0] if row else "").strip()
                if num in values:
                    data.append({"range": f"{col}{i}", "values": [[str(values[num])]]})
            if not data:
                return 0

            sheet.values().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ).execute()
            logger.info(f"Колонка «{col_name}»: обновлено {len(data)} ячеек одним запросом")
            return len(data)

        try:
            return await self._sheets_retry(_do)
        except Exception as e:
            logger.error(f"Ошибка batch-обновления колонки «{col_name}»: {e}", exc_info=True)
            return 0

    # Старая раскладка блока реквизитов — шесть колонок, стоявших там же, где
    # теперь стоят восемь новых. Нужна ровно один раз, при миграции журнала.
    LEGACY_BANK_COLUMNS = [
        "bank_corr_line1", "bank_corr_line2", "bank_corr_line3",
        "bank_ben_line1", "bank_ben_line2", "bank_kpp",
    ]

    async def migrate_requisites(self) -> dict:
        """Разовая идемпотентная миграция блока реквизитов в журнале.

        Читает шесть старых колонок, раскладывает их в восемь новых и
        переписывает заголовки. Повторный запуск на уже мигрированной таблице
        стоит один запрос на чтение и ничего не пишет.

        Перед записью проверяет, что под новый блок физически хватает места:
        восемь колонок пишутся на место шести, поэтому в таблицу должны быть
        вставлены две пустые колонки перед «Комментарий». Если их нет, запись
        затёрла бы «Комментарий» и «Платежи» — в этом случае миграция
        отказывается работать и говорит, что сделать.
        """
        first = COLUMNS.index("account_type")
        last_col = self._col_letter(len(COLUMNS) - 1)

        def _do():
            svc = self._get_service()
            sheet = svc.spreadsheets()

            header = sheet.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"A2:{last_col}2",
            ).execute().get("values", [[]])
            header = (header[0] if header else []) + [""] * len(COLUMNS)

            if str(header[first]).strip() == "account_type":
                logger.info("Миграция реквизитов: журнал уже в новой раскладке")
                return {"status": "already_migrated"}

            # Две колонки, которые должны быть вставлены вручную.
            tail = [str(header[first + 6]).strip(), str(header[first + 7]).strip()]
            if any(t and t not in self.LEGACY_BANK_COLUMNS for t in tail):
                msg = (f"в журнале не хватает двух колонок перед «Комментарий» "
                       f"(на их месте: {tail}). Вставь две пустые колонки после "
                       f"«bank_kpp» и перезапусти бота.")
                logger.error(f"Миграция реквизитов ОТМЕНЕНА: {msg}")
                return {"error": msg}

            rows = sheet.values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"A{DATA_START_ROW}:{last_col}",
            ).execute().get("values", [])

            writes = [
                {"range": f"{self._col_letter(first + i)}2", "values": [[name]]}
                for i, name in enumerate(COLUMNS[first:first + 8])
            ]

            migrated = 0
            for n, row in enumerate(rows, start=DATA_START_ROW):
                padded = row + [""] * (len(COLUMNS) - len(row))
                if not str(padded[0]).strip():
                    continue
                legacy = dict(zip(self.LEGACY_BANK_COLUMNS, padded[first:first + 6]))
                if not any(str(v).strip() for v in legacy.values()):
                    continue
                new_values = br.from_legacy(legacy)
                for i, col in enumerate(COLUMNS[first:first + 8]):
                    writes.append({
                        "range": f"{self._col_letter(first + i)}{n}",
                        "values": [[str(new_values.get(col, ""))]],
                    })
                migrated += 1

            for i in range(0, len(writes), 500):
                sheet.values().batchUpdate(
                    spreadsheetId=SPREADSHEET_ID,
                    body={"valueInputOption": "USER_ENTERED", "data": writes[i:i + 500]},
                ).execute()
            logger.info(f"Миграция реквизитов: сделок переведено {migrated}, "
                        f"ячеек записано {len(writes)}")
            return {"migrated": migrated, "cells": len(writes)}

        try:
            return await self._sheets_retry(_do) or {}
        except Exception as e:
            logger.error(f"Ошибка миграции реквизитов: {e}", exc_info=True)
            return {"error": str(e)}

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

    async def get_all_deals(self) -> list[dict]:
        """Возвращает все сделки из журнала. Явная обёртка над find_deal("").

        Используется агрегирующими инструментами (статистика, отчёты).
        Отдельный метод — чтобы вызовы «прочитать всё» читались в коде
        как намерение, а не как пустой поиск.
        """
        return await self.find_deal("")
