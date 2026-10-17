"""Google Drive uploads and folder creation using the user's OAuth account."""
import io
import logging
import os
import threading
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger(__name__)
_service = None
_service_lock = threading.Lock()


def is_configured() -> bool:
    return all(
        os.environ.get(key)
        for key in (
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_OAUTH_REFRESH_TOKEN",
        )
    )


def _svc():
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                if not is_configured():
                    raise ValueError(
                        "Google Drive OAuth is not configured. Set "
                        "GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, "
                        "and GOOGLE_OAUTH_REFRESH_TOKEN."
                    )
                credentials = Credentials(
                    token=None,
                    refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
                    client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
                    client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
                    token_uri="https://oauth2.googleapis.com/token",
                    scopes=["https://www.googleapis.com/auth/drive"],
                )
                credentials.refresh(Request())
                _service = build(
                    "drive", "v3", credentials=credentials, cache_discovery=False
                )
    return _service


def create_folder(name: str, parent_id: str) -> str:
    if not parent_id:
        raise ValueError("TREND_DRIVE_PARENT_ID is not configured.")
    metadata = {
        "name": name.strip(),
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    return _svc().files().create(body=metadata, fields="id").execute()["id"]


def upload_image(folder_id: str, filename: str, data: bytes, mimetype: str) -> str:
    if not folder_id:
        raise ValueError("A Drive destination folder is required.")
    safe_filename = Path(filename).name or "image.jpg"
    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype=mimetype or "image/jpeg",
        resumable=False,
    )
    metadata = {"name": safe_filename, "parents": [folder_id]}
    return (
        _svc()
        .files()
        .create(body=metadata, media_body=media, fields="id")
        .execute()["id"]
    )
