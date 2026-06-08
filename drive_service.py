import os
import json
import logging
import asyncio
from pathlib import Path
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

MONTHS_RU = {
    "01": "Январь", "02": "Февраль", "03": "Март",
    "04": "Апрель", "05": "Май", "06": "Июнь",
    "07": "Июль", "08": "Август", "09": "Сентябрь",
    "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь"
}


class GoogleDriveService:
    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self):
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            raise ValueError("GOOGLE_CREDENTIALS_JSON не задан")
        creds_data = json.loads(creds_json)
        self.creds = Credentials.from_service_account_info(creds_data, scopes=self.SCOPES)
        self.service = build("drive", "v3", credentials=self.creds)
        self.root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

    async def get_next_contract_number(self) -> str:
        """
        Возвращает следующий номер договора в формате ДДММГГ+NNN.
        Смотрит папку сегодняшнего дня на Drive и берёт max(NNN) + 1.
        Если папок за сегодня нет — возвращает ДДММГГ001.
        """
        from datetime import datetime
        today = datetime.now()
        day   = today.strftime("%d")
        month = today.strftime("%m")
        year  = today.strftime("%y")          # две цифры: 26
        year4 = today.strftime("%Y")          # четыре: 2026
        prefix = f"{day}{month}{year}"        # например 080626
        month_name = f"{month}-{MONTHS_RU.get(month, month)}"

        def _find_max_number():
            try:
                # Спускаемся по структуре: Договоры → 2026 → 06-Июнь
                def find_folder(name, parent):
                    q = (f"name='{name}' and "
                         f"mimeType='application/vnd.google-apps.folder' and "
                         f"'{parent}' in parents and trashed=false")
                    res = self.service.files().list(q=q, fields="files(id)").execute()
                    files = res.get("files", [])
                    return files[0]["id"] if files else None

                contracts_id = find_folder("Договоры", self.root_folder_id)
                if not contracts_id:
                    return f"{prefix}001"

                year_id = find_folder(year4, contracts_id)
                if not year_id:
                    return f"{prefix}001"

                month_id = find_folder(month_name, year_id)
                if not month_id:
                    return f"{prefix}001"

                # Ищем все папки с именем, начинающимся на сегодняшний prefix
                q = (f"mimeType='application/vnd.google-apps.folder' and "
                     f"'{month_id}' in parents and trashed=false and "
                     f"name contains '{prefix}'")
                res = self.service.files().list(q=q, fields="files(name)").execute()
                folders = res.get("files", [])

                max_n = 0
                for f in folders:
                    name = f.get("name", "")
                    if name.startswith(prefix) and len(name) == len(prefix) + 3:
                        try:
                            n = int(name[len(prefix):])
                            max_n = max(max_n, n)
                        except ValueError:
                            pass

                return f"{prefix}{max_n + 1:03d}"

            except Exception as e:
                logger.error(f"Ошибка получения номера договора: {e}", exc_info=True)
                # Fallback: дата + случайные цифры чтобы не блокировать сделку
                from datetime import datetime as dt
                return f"{prefix}{dt.now().strftime('%H%M')}"

        return await asyncio.to_thread(_find_max_number)

    async def get_or_create_deal_folder(self, contract_number: str) -> str:
        month = contract_number[2:4]
        year  = "20" + contract_number[4:6]
        month_name = f"{month}-{MONTHS_RU.get(month, month)}"

        contracts_id = await self._get_or_create_folder("Договоры", self.root_folder_id)
        year_id      = await self._get_or_create_folder(year, contracts_id)
        month_id     = await self._get_or_create_folder(month_name, year_id)
        deal_id      = await self._get_or_create_folder(contract_number, month_id)
        await self._get_or_create_folder("Сканы", deal_id)

        return deal_id

    async def upload_file(self, filepath: str, filename: str, folder_id: str,
                          mime_type: str = None) -> str:
        """Загружает файл в указанную папку, возвращает webViewLink."""
        path = Path(filepath)
        if not path.exists():
            logger.error(f"Файл не существует: {filepath}")
            return ""
        if path.stat().st_size == 0:
            logger.error(f"Файл пустой: {filepath}")
            return ""

        logger.info(f"Загружаю '{filename}' ({path.stat().st_size} байт) → папка {folder_id}")

        def _do_upload():
            nonlocal mime_type
            if mime_type is None:
                ext = filepath.rsplit(".", 1)[-1].lower()
                mime_map = {
                    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "pdf":  "application/pdf",
                }
                mime_type = mime_map.get(ext, "application/octet-stream")

            file_metadata = {"name": filename, "parents": [folder_id]}
            # FIX: resumable=False — простая загрузка, не требует next_chunk().
            # resumable=True без next_chunk() создаёт файл без содержимого.
            media = MediaFileUpload(filepath, mimetype=mime_type, resumable=False)

            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id,webViewLink"
            ).execute()

            self.service.permissions().create(
                fileId=file["id"],
                body={"type": "anyone", "role": "reader"}
            ).execute()

            return file.get("webViewLink", "")

        try:
            # FIX: Drive API синхронный — запускаем в потоке чтобы не блокировать event loop
            link = await asyncio.to_thread(_do_upload)
            logger.info(f"Загружено: {filename} → {link}")
            return link
        except Exception as e:
            logger.error(f"Ошибка загрузки '{filename}': {e}", exc_info=True)
            return ""

    async def _get_or_create_folder(self, name: str, parent_id: str) -> str:
        def _do_find_or_create():
            query = (
                f"name='{name}' and "
                f"mimeType='application/vnd.google-apps.folder' and "
                f"'{parent_id}' in parents and trashed=false"
            )
            results = self.service.files().list(q=query, fields="files(id)").execute()
            files = results.get("files", [])
            if files:
                return files[0]["id"]

            folder = self.service.files().create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id]
                },
                fields="id"
            ).execute()
            logger.info(f"Создана папка: {name}")
            return folder["id"]

        try:
            return await asyncio.to_thread(_do_find_or_create)
        except Exception as e:
            logger.error(f"Ошибка создания папки '{name}': {e}", exc_info=True)
            return parent_id
