import os
import json
import logging
import asyncio
from pathlib import Path
from datetime import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

MONTHS_RU = {
    "01": "Январь", "02": "Февраль", "03": "Март",
    "04": "Апрель", "05": "Май",     "06": "Июнь",
    "07": "Июль",   "08": "Август",  "09": "Сентябрь",
    "10": "Октябрь","11": "Ноябрь",  "12": "Декабрь"
}


def _build_service():
    """
    Создаёт Google Drive service.
    Приоритет: GOOGLE_OAUTH_TOKEN (OAuth пользователя) → GOOGLE_CREDENTIALS_JSON (сервисный аккаунт).
    OAuth не имеет проблем с квотой — файлы сохраняются как загруженные владельцем аккаунта.
    """
    oauth_token = os.environ.get("GOOGLE_OAUTH_TOKEN")
    if oauth_token:
        token_data = json.loads(oauth_token)
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/drive"]),
        )
        # Обновляем токен если истёк
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("OAuth токен обновлён")
            except Exception as e:
                logger.warning(f"Не удалось обновить токен: {e}")
        logger.info("Drive: используем OAuth (пользовательский аккаунт)")
        return build("drive", "v3", credentials=creds)

    # Fallback: сервисный аккаунт
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        from google.oauth2.service_account import Credentials as SACredentials
        creds_data = json.loads(creds_json)
        creds = SACredentials.from_service_account_info(
            creds_data,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        logger.warning("Drive: используем сервисный аккаунт (могут быть проблемы с квотой)")
        return build("drive", "v3", credentials=creds)

    raise ValueError("Не задан ни GOOGLE_OAUTH_TOKEN, ни GOOGLE_CREDENTIALS_JSON")


class GoogleDriveService:

    def __init__(self):
        self.service = _build_service()
        self.root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

    async def check_access(self) -> bool:
        """
        Быстрая проверка доступа к Drive — пробует получить метаданные корневой папки.
        Возвращает True если доступ есть, False если токен протух или нет сети.
        """
        def _do_check():
            self.service.files().get(
                fileId=self.root_folder_id,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            return True

        try:
            return await asyncio.to_thread(_do_check)
        except Exception as e:
            logger.error(f"Drive недоступен: {e}")
            # Пробуем пересоздать service (обновить токен) и проверить ещё раз
            try:
                self.service = _build_service()
                return await asyncio.to_thread(_do_check)
            except Exception as e2:
                logger.error(f"Drive недоступен после пересоздания service: {e2}")
                return False

    async def get_next_contract_number(self) -> str:
        """
        Возвращает следующий номер договора в формате ДДММГГ+NNN.
        Смотрит папку сегодняшнего дня на Drive и берёт max(NNN) + 1.
        Если папок за сегодня нет — возвращает ДДММГГ001.
        """
        today = datetime.now()
        day   = today.strftime("%d")
        month = today.strftime("%m")
        year  = today.strftime("%y")
        year4 = today.strftime("%Y")
        prefix = f"{day}{month}{year}"
        month_name = f"{month}-{MONTHS_RU.get(month, month)}"

        def _find_max_number():
            try:
                def find_folder(name, parent):
                    q = (f"name='{name}' and "
                         f"mimeType='application/vnd.google-apps.folder' and "
                         f"'{parent}' in parents and trashed=false")
                    res = self.service.files().list(q=q, fields="files(id)",
                                                    supportsAllDrives=True,
                                                    includeItemsFromAllDrives=True).execute()
                    files = res.get("files", [])
                    return files[0]["id"] if files else None

                contracts_id = find_folder("Договоры", self.root_folder_id)
                if not contracts_id: return f"{prefix}001"

                year_id = find_folder(year4, contracts_id)
                if not year_id: return f"{prefix}001"

                month_id = find_folder(month_name, year_id)
                if not month_id: return f"{prefix}001"

                q = (f"mimeType='application/vnd.google-apps.folder' and "
                     f"'{month_id}' in parents and trashed=false and "
                     f"name contains '{prefix}'")
                res = self.service.files().list(q=q, fields="files(name)",
                                                supportsAllDrives=True,
                                                includeItemsFromAllDrives=True).execute()
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
                return f"{prefix}001"

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
            media = MediaFileUpload(filepath, mimetype=mime_type, resumable=False)

            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            ).execute()

            try:
                self.service.permissions().create(
                    fileId=file["id"],
                    body={"type": "anyone", "role": "reader"},
                    supportsAllDrives=True,
                ).execute()
            except Exception as e:
                logger.warning(f"Не удалось открыть доступ к файлу: {e}")

            return file.get("webViewLink", "")

        for attempt in range(3):
            try:
                link = await asyncio.to_thread(_do_upload)
                logger.info(f"Загружено: {filename} → {link}")
                return link
            except Exception as e:
                logger.warning(f"Попытка {attempt+1}/3 загрузки '{filename}' не удалась: {e}")
                if attempt < 2:
                    # Пересоздаём service — старое SSL-соединение могло сломаться
                    self.service = _build_service()
                    await asyncio.sleep(1)
                else:
                    logger.error(f"Окончательная ошибка загрузки '{filename}': {e}", exc_info=True)
                    return ""
        return ""

    async def _get_or_create_folder(self, name: str, parent_id: str) -> str:
        def _do_find_or_create():
            query = (
                f"name='{name}' and "
                f"mimeType='application/vnd.google-apps.folder' and "
                f"'{parent_id}' in parents and trashed=false"
            )
            results = self.service.files().list(
                q=query, fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            files = results.get("files", [])
            if files:
                return files[0]["id"]

            folder = self.service.files().create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                },
                fields="id",
                supportsAllDrives=True,
            ).execute()
            logger.info(f"Создана папка: {name}")
            return folder["id"]

        try:
            return await asyncio.to_thread(_do_find_or_create)
        except Exception as e:
            logger.error(f"Ошибка создания папки '{name}': {e}", exc_info=True)
            return parent_id
