import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

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

    async def get_or_create_deal_folder(self, contract_number: str) -> str:
        """
        Создаёт цепочку папок:
        Авто Континент / Договоры / 2026 / 06-Июнь / 080626001 / 
        и подпапку Сканы внутри.
        Возвращает ID папки сделки.
        """
        # Парсим дату из номера ДДММГГ + NNN
        day   = contract_number[0:2]
        month = contract_number[2:4]
        year  = "20" + contract_number[4:6]
        month_name = f"{month}-{MONTHS_RU.get(month, month)}"

        contracts_id = await self._get_or_create_folder("Договоры",   self.root_folder_id)
        year_id      = await self._get_or_create_folder(year,          contracts_id)
        month_id     = await self._get_or_create_folder(month_name,    year_id)
        deal_id      = await self._get_or_create_folder(contract_number, month_id)

        # Создаём подпапку Сканы
        await self._get_or_create_folder("Сканы", deal_id)

        return deal_id

    async def upload_file(self, filepath: str, filename: str, folder_id: str,
                          mime_type: str = None) -> str:
        """Загружает файл в указанную папку, возвращает ссылку."""
        try:
            if mime_type is None:
                ext = filepath.rsplit(".", 1)[-1].lower()
                mime_map = {
                    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "pdf":  "application/pdf",
                }
                mime_type = mime_map.get(ext, "application/octet-stream")

            file_metadata = {"name": filename, "parents": [folder_id]}
            media = MediaFileUpload(filepath, mimetype=mime_type, resumable=True)

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

        except Exception as e:
            print(f"Ошибка загрузки '{filename}': {e}")
            return ""

    async def _get_or_create_folder(self, name: str, parent_id: str) -> str:
        try:
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
            return folder["id"]

        except Exception as e:
            print(f"Ошибка создания папки '{name}': {e}")
            return parent_id
