"""
sheets/settings.py — App settings (Settings tab, key | value).

Owner ke controls yahan rehte hain taaki woh Pins app se badle jaa saken —
bina redeploy, bina code chhuye. Pehla setting: vision feeder ki daily
image limit (`vision_daily_limit`), jo app ke Vision screen se set hoti hai.

Reads 60s in-memory cache ke saath (feeder loop har cycle me padhta hai,
Sheets API par bojh nahi). Koi bhi error par caller ko None milta hai —
caller apna default use karega. Writes throttled base helpers se.
"""
import logging
import time

from sheets.base import _open_worksheet, _throttled_read, _throttled_write

logger = logging.getLogger(__name__)

_TAB = "Settings"
_cache: dict = {"ts": 0.0, "values": {}}
_CACHE_TTL = 60.0  # seconds


def _load_all() -> dict:
    """Saari settings ek dict me (cache ke saath). Error → purana cache / {}."""
    now = time.monotonic()
    if now - _cache["ts"] < _CACHE_TTL and _cache["values"]:
        return _cache["values"]
    try:
        def _read():
            return _open_worksheet(_TAB).get_all_records()

        values = {}
        for row in _throttled_read(_read):
            key = str(row.get("key", "")).strip()
            if key:
                values[key] = str(row.get("value", "")).strip()
        _cache["values"] = values
        _cache["ts"] = now
        return values
    except Exception as e:
        logger.warning(f"Settings load failed — {type(e).__name__}: {e}")
        return _cache["values"]


def get_setting(key: str) -> str | None:
    """Ek setting ki value, ya None agar set nahi / sheet down."""
    return _load_all().get(key)


def get_int_setting(key: str, default: int, lo: int, hi: int) -> int:
    """Integer setting — parse/clamp ke saath; kuch bhi gadbad ho toh default."""
    raw = get_setting(key)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def set_setting(key: str, value: str) -> bool:
    """Setting likho (row update ya append). Safal ho toh True + cache turant fresh."""
    def _write():
        sheet = _open_worksheet(_TAB)
        records = sheet.get_all_records()
        for i, row in enumerate(records):
            if str(row.get("key", "")).strip() == key:
                sheet.update(f"B{i + 2}", [[value]])
                return
        sheet.append_row([key, value])

    try:
        _throttled_write(_write)
        _cache["values"][key] = value
        _cache["ts"] = time.monotonic()
        return True
    except Exception as e:
        logger.warning(f"Settings write failed ({key}) — {type(e).__name__}: {e}")
        return False
