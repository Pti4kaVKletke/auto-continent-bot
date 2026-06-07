import os
import json
from pathlib import Path
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


class GoogleDriveService:
    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self):
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            raise ValueError("GOOGLE_CREDENTIALS_JSON не задан в переменных окружения")

        creds_data = json.loads(creds_json)
        self.creds = Credentials.from_service_account_info(creds_data, scopes=self.SCOPES)
        self.service = build("drive", "v3", credentials=self.creds)

        # ID корневой папки на Google Drive (задайте в переменных окружения)
        self.root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

    async def upload_file(self, filepath: str, filename: str, folder: str = None) -> str:
        """Загружаем файл на Google Drive, возвращаем ссылку"""
        try:
            folder_id = await self._get_or_create_folder(folder) if folder else self.root_folder_id

            file_metadata = {
                "name": filename,
                "parents": [folder_id] if folder_id else []
            }

            media = MediaFileUpload(
                filepath,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                resumable=True
            )

            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id,webViewLink"
            ).execute()

            # Открываем доступ по ссылке
            self.service.permissions().create(
                fileId=file["id"],
                body={"type": "anyone", "role": "reader"}
            ).execute()

            return file.get("webViewLink", "")

        except Exception as e:
            print(f"Ошибка загрузки на Drive: {e}")
            return ""

    async def _get_or_create_folder(self, folder_name: str) -> str:
        """Находим или создаём папку"""
        try:
            # Ищем существующую
            query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            if self.root_folder_id:
                query += f" and '{self.root_folder_id}' in parents"

            results = self.service.files().list(q=query, fields="files(id)").execute()
            files = results.get("files", [])

            if files:
                return files[0]["id"]

            # Создаём новую
            folder_metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder"
            }
            if self.root_folder_id:
                folder_metadata["parents"] = [self.root_folder_id]

            folder = self.service.files().create(
                body=folder_metadata,
                fields="id"
            ).execute()

            return folder["id"]

        except Exception as e:
            print(f"Ошибка создания папки: {e}")
            return self.root_folder_id
