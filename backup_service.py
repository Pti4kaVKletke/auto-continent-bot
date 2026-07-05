"""
Ежедневный бэкап журнала сделок в Google Drive.

Экспортирует Google Sheet в xlsx через Drive API и загружает в подпапку
`Бэкапы журнала` внутри корневой рабочей папки Drive. Ротация: удаляет
файлы старше BACKUP_KEEP_DAYS.

Все методы синхронные — bot.py оборачивает в asyncio.to_thread. Так проще
и не тянет aiogoogle. Файлы небольшие (десятки-сотни KB), блокировка < 1 сек.
"""

import io
import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)

SPREADSHEET_ID     = os.environ.get("GOOGLE_SHEETS_ID",       "1OHkExAxQzm_3kiOE-h4aGug-MO3yf4OODB8C_fACz08")
ROOT_FOLDER_ID     = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "1GbMLXVtAyh4fsfy1UBDRE0xyuJzKVGN1")
BACKUP_FOLDER_NAME = os.environ.get("BACKUP_FOLDER_NAME",     "Бэкапы журнала")
BACKUP_KEEP_DAYS   = int(os.environ.get("BACKUP_KEEP_DAYS",   "30"))
BACKUP_FILE_PREFIX = "journal_backup_"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _save_token_to_railway(creds: Credentials, original_token_data: dict) -> None:
    """Сохраняет обновлённый OAuth-токен обратно в Railway (аналогично drive/sheets)."""
    try:
        updated = dict(original_token_data)
        updated["token"] = creds.token
        if creds.expiry:
            updated["token_expiry"] = creds.expiry.isoformat()
        new_value = json.dumps(updated)

        railway_token      = os.environ.get("RAILWAY_API_TOKEN", "")
        railway_service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
        if not railway_token or not railway_service_id:
            logger.warning("RAILWAY_API_TOKEN или RAILWAY_SERVICE_ID не заданы — токен Backup не сохранён")
            return

        query = """
        mutation UpsertVariables($input: ServiceVariablesInput!) {
          serviceVariablesUpsert(input: $input)
        }
        """
        variables = {
            "input": {
                "serviceId":     railway_service_id,
                "environmentId": os.environ.get("RAILWAY_ENVIRONMENT_ID", ""),
                "variables":     {"GOOGLE_OAUTH_TOKEN": new_value},
            }
        }
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(
            "https://backboard.railway.app/graphql/v2",
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {railway_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if "errors" in result:
                logger.warning(f"Railway API (Backup): ошибки: {result['errors']}")
            else:
                os.environ["GOOGLE_OAUTH_TOKEN"] = new_value
                logger.info("Backup OAuth токен сохранён в Railway")
    except Exception as e:
        logger.warning(f"Не удалось сохранить Backup токен в Railway: {e}")


def _build_credentials() -> Credentials:
    oauth_token = os.environ.get("GOOGLE_OAUTH_TOKEN")
    if not oauth_token:
        raise ValueError("GOOGLE_OAUTH_TOKEN не задан")
    token_data = json.loads(oauth_token)
    creds = Credentials(
        token         = token_data.get("token"),
        refresh_token = token_data.get("refresh_token"),
        token_uri     = token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id     = token_data.get("client_id"),
        client_secret = token_data.get("client_secret"),
        scopes        = token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info("Backup OAuth токен обновлён")
            _save_token_to_railway(creds, token_data)
        except Exception as e:
            logger.warning(f"Не удалось обновить токен для Backup: {e}")
    return creds


def _escape_drive_query_string(s: str) -> str:
    """Экранирует одинарные кавычки для Drive query."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


class BackupService:
    """Синхронный сервис бэкапов. Bot.py оборачивает вызовы в asyncio.to_thread."""

    def __init__(self):
        self._drive = None
        self._folder_id_cache: str | None = None

    def _get_drive(self):
        if self._drive is None:
            self._drive = build("drive", "v3", credentials=_build_credentials())
        return self._drive

    def _reset(self):
        """Сброс кэша сервиса и папки — используется при 401/токен-ошибках."""
        self._drive = None
        self._folder_id_cache = None

    # ── Папка бэкапов ────────────────────────────────────────────────────

    def _get_or_create_backup_folder(self) -> str:
        if self._folder_id_cache:
            return self._folder_id_cache

        drive = self._get_drive()
        safe_name = _escape_drive_query_string(BACKUP_FOLDER_NAME)
        q = (
            f"name = '{safe_name}' and "
            f"'{ROOT_FOLDER_ID}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )
        res = drive.files().list(
            q=q,
            fields="files(id, name)",
            spaces="drive",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = res.get("files", [])
        if files:
            self._folder_id_cache = files[0]["id"]
            return self._folder_id_cache

        folder = drive.files().create(
            body={
                "name":     BACKUP_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
                "parents":  [ROOT_FOLDER_ID],
            },
            fields="id",
            supportsAllDrives=True,
        ).execute()
        self._folder_id_cache = folder["id"]
        logger.info(f"Создана папка бэкапов: {BACKUP_FOLDER_NAME} (id={folder['id']})")
        return self._folder_id_cache

    # ── Создание бэкапа ──────────────────────────────────────────────────

    def create_backup(self) -> dict:
        """Экспорт Sheet → xlsx → загрузка в папку бэкапов.

        Возвращает: {success, file_id, file_name, size_kb, web_link, folder_id}
        или {success: False, error: "..."}.
        """
        try:
            drive     = self._get_drive()
            folder_id = self._get_or_create_backup_folder()

            # 1. Экспорт Google Sheet как xlsx bytes
            xlsx_bytes = drive.files().export(
                fileId=SPREADSHEET_ID,
                mimeType=XLSX_MIME,
            ).execute()
            size_kb = round(len(xlsx_bytes) / 1024, 1)

            # 2. Загрузка в папку бэкапов
            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_name = f"{BACKUP_FILE_PREFIX}{ts}.xlsx"

            media = MediaIoBaseUpload(
                io.BytesIO(xlsx_bytes),
                mimetype=XLSX_MIME,
                resumable=False,
            )
            uploaded = drive.files().create(
                body={"name": file_name, "parents": [folder_id]},
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            ).execute()

            logger.info(f"Бэкап создан: {file_name} ({size_kb} KB, id={uploaded['id']})")
            return {
                "success":   True,
                "file_id":   uploaded["id"],
                "file_name": file_name,
                "size_kb":   size_kb,
                "web_link":  uploaded.get("webViewLink", ""),
                "folder_id": folder_id,
            }
        except Exception as e:
            logger.error(f"Ошибка создания бэкапа: {e}", exc_info=True)
            # Одноразовый retry на случай протухшего сервиса
            self._reset()
            return {"success": False, "error": str(e)}

    # ── Очистка старых ───────────────────────────────────────────────────

    def cleanup_old_backups(self, keep_days: int | None = None) -> dict:
        """Удаляет бэкапы старше keep_days. Возвращает {success, deleted, kept_days}."""
        days = keep_days if keep_days is not None else BACKUP_KEEP_DAYS
        try:
            drive     = self._get_drive()
            folder_id = self._get_or_create_backup_folder()

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            # Drive API ждёт RFC 3339 UTC
            cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

            q = (
                f"'{folder_id}' in parents and "
                f"name contains '{BACKUP_FILE_PREFIX}' and "
                f"createdTime < '{cutoff_str}' and "
                "trashed = false"
            )
            res = drive.files().list(
                q=q,
                fields="files(id, name, createdTime)",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            files = res.get("files", [])

            deleted, failed = 0, 0
            for f in files:
                try:
                    drive.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                    deleted += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"Не удалось удалить {f['name']}: {e}")

            if deleted:
                logger.info(f"Очистка: удалено {deleted} бэкапов старше {days} дн.")
            return {"success": True, "deleted": deleted, "failed": failed, "kept_days": days}

        except Exception as e:
            logger.error(f"Ошибка очистки старых бэкапов: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ── Список ───────────────────────────────────────────────────────────

    def list_backups(self, limit: int = 10) -> list[dict]:
        """Возвращает последние N бэкапов (свежие сверху).
        Каждый элемент: {id, name, created, size_kb, web_link}.
        """
        try:
            drive     = self._get_drive()
            folder_id = self._get_or_create_backup_folder()

            res = drive.files().list(
                q=(
                    f"'{folder_id}' in parents and "
                    f"name contains '{BACKUP_FILE_PREFIX}' and "
                    "trashed = false"
                ),
                orderBy="createdTime desc",
                pageSize=limit,
                fields="files(id, name, createdTime, size, webViewLink)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()

            files = res.get("files", [])
            result = []
            for f in files:
                created_raw = f.get("createdTime", "")
                created_disp = ""
                try:
                    # 2026-07-04T18:15:00.000Z → 04.07.2026 18:15
                    dt = datetime.strptime(created_raw[:19], "%Y-%m-%dT%H:%M:%S")
                    created_disp = dt.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    created_disp = created_raw

                size_raw = f.get("size")
                size_kb = round(int(size_raw) / 1024, 1) if size_raw else 0
                result.append({
                    "id":       f["id"],
                    "name":     f["name"],
                    "created":  created_disp,
                    "size_kb":  size_kb,
                    "web_link": f.get("webViewLink", ""),
                })
            return result

        except Exception as e:
            logger.error(f"Ошибка получения списка бэкапов: {e}", exc_info=True)
            return []

    def get_folder_link(self) -> str:
        """Прямая ссылка на папку бэкапов в Drive."""
        try:
            fid = self._get_or_create_backup_folder()
            return f"https://drive.google.com/drive/folders/{fid}"
        except Exception as e:
            logger.warning(f"Не удалось получить ссылку на папку бэкапов: {e}")
            return ""
