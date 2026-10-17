"""Trend settings and visual-prompt data stored in dedicated Google Sheets tabs."""
import json
import logging
import threading
import time
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_CREDS_JSON, SPREADSHEET_ID

logger = logging.getLogger(__name__)

SETTINGS_TAB = "Trend_Settings"
PROMPTS_TAB = "Trend_Prompts"
SETTINGS_HEADERS = ["trend", "active", "drive_folder_id", "keywords"]
PROMPTS_HEADERS = [
    "trend",
    "style_key",
    "account",
    "label",
    "description",
    "t2i_base",
    "niche_affinity",
    "source_file",
    "added_on",
]
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_lock = threading.RLock()
_client_lock = threading.Lock()
_client = None
_cache = {"settings": (0.0, []), "prompts": (0.0, [])}
_TTL = 60


def _gc():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not GOOGLE_CREDS_JSON:
                    raise ValueError("GOOGLE_CREDS_JSON is not configured.")
                if not SPREADSHEET_ID:
                    raise ValueError("SPREADSHEET_ID is not configured.")
                info = (
                    json.loads(GOOGLE_CREDS_JSON)
                    if isinstance(GOOGLE_CREDS_JSON, str)
                    else GOOGLE_CREDS_JSON
                )
                _client = gspread.authorize(
                    Credentials.from_service_account_info(info, scopes=_SCOPES)
                )
    return _client


def _ws(tab: str, headers: list):
    spreadsheet = _gc().open_by_key(SPREADSHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(tab)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=tab, rows=500, cols=len(headers)
        )
    if not worksheet.row_values(1):
        worksheet.append_row(headers)
    return worksheet


def init_trend_tabs():
    """Create both trend tabs on startup without touching regular prompt tabs."""
    _ws(SETTINGS_TAB, SETTINGS_HEADERS)
    _ws(PROMPTS_TAB, PROMPTS_HEADERS)


def _clear():
    _cache["settings"] = (0.0, [])
    _cache["prompts"] = (0.0, [])


def _is_true(value) -> bool:
    return str(value).strip().upper() == "TRUE"


def _string(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def get_trends() -> list:
    cached_at, data = _cache["settings"]
    if cached_at and time.monotonic() - cached_at < _TTL:
        return [dict(item) for item in data]

    rows = _ws(SETTINGS_TAB, SETTINGS_HEADERS).get_all_records()
    trends = []
    for row in rows:
        name = str(row.get("trend", "")).strip()
        if not name:
            continue
        trends.append(
            {
                "trend": name,
                "active": _is_true(row.get("active")),
                "drive_folder_id": str(row.get("drive_folder_id", "")).strip(),
                "keywords": [
                    keyword.strip()
                    for keyword in str(row.get("keywords", "")).split(",")
                    if keyword.strip()
                ],
            }
        )
    _cache["settings"] = (time.monotonic(), trends)
    return [dict(item) for item in trends]


def get_active_trends() -> list:
    return [trend for trend in get_trends() if trend["active"]]


def _find_row(worksheet, trend: str) -> int:
    wanted = trend.strip().casefold()
    for row_number, value in enumerate(worksheet.col_values(1), start=1):
        if row_number > 1 and value.strip().casefold() == wanted:
            return row_number
    return 0


def add_trend(trend: str, keywords: str = "", drive_folder_id: str = "") -> bool:
    name = trend.strip()
    if not name:
        return False
    with _lock:
        worksheet = _ws(SETTINGS_TAB, SETTINGS_HEADERS)
        if _find_row(worksheet, name):
            return False
        worksheet.append_row([name, "FALSE", drive_folder_id.strip(), keywords.strip()])
        _clear()
        return True


def set_trend_active(trend: str, active: bool) -> bool:
    with _lock:
        worksheet = _ws(SETTINGS_TAB, SETTINGS_HEADERS)
        row = _find_row(worksheet, trend)
        if not row:
            return False
        worksheet.update_cell(row, 2, "TRUE" if active else "FALSE")
        _clear()
        return True


def set_trend_keywords(trend: str, keywords: str) -> bool:
    with _lock:
        worksheet = _ws(SETTINGS_TAB, SETTINGS_HEADERS)
        row = _find_row(worksheet, trend)
        if not row:
            return False
        worksheet.update_cell(row, 4, keywords.strip())
        _clear()
        return True


def _all_prompts() -> list:
    cached_at, data = _cache["prompts"]
    if cached_at and time.monotonic() - cached_at < _TTL:
        return [dict(item) for item in data]
    rows = _ws(PROMPTS_TAB, PROMPTS_HEADERS).get_all_records()
    _cache["prompts"] = (time.monotonic(), rows)
    return [dict(item) for item in rows]


def get_trend_prompts(trend: str) -> list:
    wanted = trend.strip().casefold()
    return [
        row
        for row in _all_prompts()
        if str(row.get("trend", "")).strip().casefold() == wanted
        and str(row.get("t2i_base", "")).strip()
    ]


def processed_sources(trend: str) -> set:
    wanted = trend.strip().casefold()
    return {
        str(row.get("source_file", "")).strip()
        for row in _all_prompts()
        if str(row.get("trend", "")).strip().casefold() == wanted
        and str(row.get("source_file", "")).strip()
    }


def count_added_today() -> int:
    today = str(date.today())
    return sum(
        1
        for row in _all_prompts()
        if str(row.get("added_on", "")).strip() == today
    )


def count_prompts_by_trend() -> dict:
    counts = {}
    for row in _all_prompts():
        name = str(row.get("trend", "")).strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def append_trend_prompt(trend: str, dna: dict, source_file: str):
    with _lock:
        worksheet = _ws(PROMPTS_TAB, PROMPTS_HEADERS)
        worksheet.append_row(
            [
                trend.strip(),
                _string(dna.get("style_key")),
                _string(dna.get("account")),
                _string(dna.get("label")),
                _string(dna.get("description")),
                _string(dna.get("t2i_base")),
                _string(dna.get("niche_affinity")),
                source_file.strip(),
                str(date.today()),
            ]
        )
        _cache["prompts"] = (0.0, [])
